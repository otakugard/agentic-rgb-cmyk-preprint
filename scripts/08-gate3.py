#!/usr/bin/env python3
"""08-gate3.py <job-id> [--selftest]

Gate 3: inspect the final delivery PDF after the human exports it.
It does not create the PDF and it does not claim full PDF/X compliance;
it checks the facts this pipeline can verify from the file.

--selftest runs the same checks on work/cmyk.pdf (which is not a delivery
PDF) and writes gate3-selftest.json to prove that missing OutputIntent /
PDF-X / page boxes are detected.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import shutil

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "lib"))
import acceptance  # noqa: E402
import color  # noqa: E402
import pdfprobe  # noqa: E402
import raster  # noqa: E402

GS = shutil.which("gs") or "/opt/homebrew/bin/gs"


_PDFX_CONTRACTS = {
    "pdfx-4-2010": "PDF/X-4:2010",
    "pdfx-4": "PDF/X-4",
    "pdfx-3-2003": "PDF/X-3:2003",
    "pdfx-1a-2003": "PDF/X-1a:2003",
}


def _pdfx_target(job, spec):
    """Job contract wins; then job.pdfxStandard; then the global spec."""
    contract = str((job.get("pdfExport") or {}).get("contract") or "").strip().lower()
    if contract in _PDFX_CONTRACTS:
        return _PDFX_CONTRACTS[contract]
    return job.get("pdfxStandard") or (spec.get("print") or {}).get("pdfxStandard")


def _decoded_blob(path, raw):
    """Raw bytes plus every decodable object stream, so compressed actions are visible."""
    parts = [raw]
    objs = pdfprobe.load_object_streams(pdfprobe.load_objects(raw))
    pdfprobe.resolve_streams(raw, objs)
    for obj in objs.values():
        stream = obj.get("stream")
        if stream:
            parts.append(pdfprobe.decompress(obj["dict"], stream))
    return b"\n".join(parts)


def load(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def pt2mm(v):
    return round(v * 25.4 / 72, 2)


def trim_mm(job, spec):
    if job.get("trim", {}).get("preset"):
        w, h = spec["trimPresetsMm"][job["trim"]["preset"]]
        return float(w), float(h)
    return float(job["trim"]["w"]), float(job["trim"]["h"])


def check(cid, label, status, value, expected, evidence=""):
    return {"id": cid, "label": label, "status": status,
            "value": value, "expected": expected, "evidence": evidence}


def render_tac(pdf, dpi, vector_cmyk=None, media_box=None, crop_box=None):
    with tempfile.TemporaryDirectory() as tmp:
        tif = os.path.join(tmp, "p.tif")
        # TAC must be measured on the file's own CMYK values. FastColor keeps
        # DeviceCMYK numbers as-is; ICC conversion would change the ink totals.
        subprocess.run([GS, "-q", "-dBATCH", "-dNOPAUSE", "-dSAFER",
                        "-dUseFastColor=true",
                        "-sDEVICE=tiff32nc", "-r%d" % dpi, "-o", tif, pdf],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        arr = raster.read_tiff(tif)["data"].astype(np.int16)
    if crop_box and media_box and len(crop_box) == 4 and len(media_box) == 4:
        sc = dpi / 72.0
        x0 = max(0, int(round((crop_box[0] - media_box[0]) * sc)))
        x1 = min(arr.shape[1], int(round((crop_box[2] - media_box[0]) * sc)))
        y0 = max(0, int(round((media_box[3] - crop_box[3]) * sc)))
        y1 = min(arr.shape[0], int(round((media_box[3] - crop_box[1]) * sc)))
        arr = arr[y0:y1, x0:x1]
    tac = arr.sum(axis=2) * 100.0 / 255.0
    out = {
        "dpi": dpi,
        "maxTacPercent": round(float(tac.max()), 2),
        "shareOver300Percent": round(float((tac > 300).mean() * 100), 3),
        "shareOver330Percent": round(float((tac > 330).mean() * 100), 3),
        "pixels": int(tac.size),
    }
    if vector_cmyk:
        target = np.array(vector_cmyk, dtype=np.float32) * 255.0 / 100.0
        near = np.abs(arr.astype(np.float32) - target).max(axis=2) <= 10
        out["visibleVectorPixels"] = int(np.count_nonzero(near))
        out["vectorVisible"] = out["visibleVectorPixels"] > 0
    return out


def bleed_content(pdf, boxes, bleed_mm, dpi=300):
    media = boxes.get("MediaBox")
    trim = boxes.get("TrimBox")
    if not media or not trim or len(media) != 4 or len(trim) != 4:
        return {"status": "pending", "error": "MediaBox/TrimBox missing"}
    if pt2mm(media[2] - media[0]) <= pt2mm(trim[2] - trim[0]) + 0.5:
        return {"status": "pending", "error": "final PDF has no bleed area (MediaBox=TrimBox)"}
    with tempfile.TemporaryDirectory() as tmp:
        tif = os.path.join(tmp, "p.tif")
        subprocess.run([GS, "-q", "-dBATCH", "-dNOPAUSE", "-dSAFER", "-sDEVICE=tiff32nc",
                        "-sDefaultCMYKProfile=%s" % color.FOGRA39, "-dOverrideICC=true",
                        "-r%d" % dpi, "-o", tif, pdf], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        arr = raster.read_tiff(tif)["data"]
    scale = dpi / 72.0
    x0 = int(round((trim[0] - media[0]) * scale))
    x1 = int(round((trim[2] - media[0]) * scale))
    y0 = int(round((media[3] - trim[3]) * scale))
    y1 = int(round((media[3] - trim[1]) * scale))
    band = int(round(bleed_mm / 25.4 * dpi))
    white = np.all(arr == 0, axis=2)
    h, w = white.shape
    sides = {
        "left": white[max(0, y0):min(h, y1), max(0, x0 - band):max(0, x0)],
        "right": white[max(0, y0):min(h, y1), min(w, x1):min(w, x1 + band)],
        "top": white[max(0, y0 - band):max(0, y0), max(0, x0):min(w, x1)],
        "bottom": white[min(h, y1):min(h, y1 + band), max(0, x0):min(w, x1)],
    }
    counts = {k: int(np.count_nonzero(v)) for k, v in sides.items()}
    return {
        "status": "pass" if not any(counts.values()) else "fail",
        "dpi": dpi,
        "bandPx": band,
        "trimPx": [x0, y0, x1, y1],
        "whitePixels": counts,
    }


def visual_compare(final_pdf, ai, dpi=150, crop_mm=0.0, media_box=None, crop_box=None):
    with tempfile.TemporaryDirectory() as tmp:
        f_cmyk = os.path.join(tmp, "final-cmyk.tif")
        f_sim = os.path.join(tmp, "final-sim.tif")
        a_cmyk = os.path.join(tmp, "ai-cmyk.tif")
        a_sim = os.path.join(tmp, "ai-sim.tif")
        subprocess.run([GS, "-q", "-dBATCH", "-dNOPAUSE", "-dSAFER", "-sDEVICE=tiff32nc",
                        "-sDefaultCMYKProfile=%s" % color.FOGRA39, "-dOverrideICC=true",
                        "-r%d" % dpi, "-o", f_cmyk, final_pdf], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["tificc", "-n", "-i" + color.FOGRA39, "-o" + color.SRGB,
                        "-t1", "-b", "-w8", f_cmyk, f_sim], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run([GS, "-q", "-dBATCH", "-dNOPAUSE", "-dSAFER", "-sDEVICE=tiff32nc",
                        "-sDefaultCMYKProfile=%s" % color.FOGRA39, "-dOverrideICC=true",
                        "-r%d" % dpi, "-o", a_cmyk, ai], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["tificc", "-n", "-i" + color.FOGRA39, "-o" + color.SRGB,
                        "-t1", "-b", "-w8", a_cmyk, a_sim], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        f = raster.read_tiff(f_sim)["data"][..., :3]
        a = raster.read_tiff(a_sim)["data"][..., :3]
    if crop_box and media_box and len(crop_box) == 4 and len(media_box) == 4:
        sc = dpi / 72.0
        x0 = max(0, int(round((crop_box[0] - media_box[0]) * sc)))
        x1 = min(f.shape[1], int(round((crop_box[2] - media_box[0]) * sc)))
        y0 = max(0, int(round((media_box[3] - crop_box[3]) * sc)))
        y1 = min(f.shape[0], int(round((media_box[3] - crop_box[1]) * sc)))
        f = f[y0:y1, x0:x1]
    else:
        crop_px = int(round(crop_mm / 25.4 * dpi)) if crop_mm else 0
        h, w = f.shape[:2]
        if crop_px:
            f = raster.crop(f, crop_px, crop_px, w - crop_px, h - crop_px)
    hh = min(f.shape[0], a.shape[0])
    ww = min(f.shape[1], a.shape[1])
    f = raster.resize_nearest(f, ww, hh)
    a = raster.resize_nearest(a, ww, hh)
    return raster.compare_aligned(f, a, max_shift=2, flat=True, flat_radius=5)


def analyse(pdf, job, spec, selftest=False):
    info = pdfprobe.inspect(pdf)
    raw = open(pdf, "rb").read()
    tw, th = trim_mm(job, spec)
    b = float(job.get("bleedMm", spec["print"].get("bleedMm", 0)) or 0)
    checks = []
    boxes = info.get("boxes", {})
    st = info.get("structure", {})
    colours = info.get("colours", [])
    rgb_ops = sum(c["count"] for c in colours if c["space"] == "rgb")
    cmyk_ops = sum(c["count"] for c in colours if c["space"] == "cmyk")

    checks.append(check("pdf.exists", "Final PDF exists", "pass", os.path.basename(pdf), "exists",
                        "sha256=%s" % info["sha256"][:16]))

    # Page boxes
    for key, expected_w, expected_h in (
            ("TrimBox", tw, th), ("BleedBox", tw + 2 * b, th + 2 * b)):
        box = boxes.get(key)
        if not box or len(box) != 4:
            checks.append(check("pdf.%s" % key.lower(), key, "fail", "missing",
                                "%s x %s mm" % (expected_w, expected_h), ""))
            continue
        wmm = pt2mm(box[2] - box[0])
        hmm = pt2mm(box[3] - box[1])
        d = max(abs(wmm - expected_w), abs(hmm - expected_h))
        checks.append(check("pdf.%s" % key.lower(), key,
                            "pass" if d <= 0.5 else "fail",
                            "%s × %s mm" % (wmm, hmm),
                            "%s × %s mm" % (expected_w, expected_h),
                            "max deviation %.2f mm" % d))

    trim = boxes.get("TrimBox")
    bleed = boxes.get("BleedBox")
    layout = (job.get("pdfExport") or {}).get("layout")
    margin_pt = float((job.get("pdfExport") or {}).get("marginMm", 0) or 0) * 72 / 25.4
    if trim and len(trim) == 4:
        if layout == "marks" and margin_pt > 0:
            media = boxes.get("MediaBox") or [0, 0, 0, 0]
            pos_ok = (trim[0] >= margin_pt - 0.5
                      and trim[1] >= margin_pt - 0.5
                      and trim[2] <= media[2] - margin_pt + 0.5
                      and trim[3] <= media[3] - margin_pt + 0.5)
            expected = "at least %.1f mm inset on all four sides" % (margin_pt * 25.4 / 72)
            note = "marks layout: TrimBox is inside the marks page, not at 0,0; extra margin can hold marks"
        else:
            pos_ok = abs(trim[0]) <= 0.5 and abs(trim[1]) <= 0.5
            expected = "0, 0 pt"
            note = "Width and height alone do not catch a page box that moved as a whole"
        checks.append(check("pdf.trimposition", "TrimBox origin",
                            "pass" if pos_ok else "fail",
                            [round(trim[0], 2), round(trim[1], 2)], expected, note))
    if trim and bleed and len(trim) == 4 and len(bleed) == 4:
        bpt = b / 25.4 * 72.0
        contain_ok = (bleed[0] <= trim[0] - bpt + 0.5
                      and bleed[1] <= trim[1] - bpt + 0.5
                      and bleed[2] >= trim[2] + bpt - 0.5
                      and bleed[3] >= trim[3] + bpt - 0.5)
        checks.append(check("pdf.bleedcontain", "BleedBox contains TrimBox",
                            "pass" if contain_ok else "fail",
                            {"trim": trim, "bleed": bleed}, "%.1f mm outside on all four sides" % b, ""))
        media = boxes.get("MediaBox")
        if media and len(media) == 4:
            media_ok = (media[0] <= bleed[0] + 0.5 and media[1] <= bleed[1] + 0.5
                        and media[2] >= bleed[2] - 0.5 and media[3] >= bleed[3] - 0.5)
            checks.append(check("pdf.mediabox", "MediaBox contains BleedBox",
                                "pass" if media_ok else "fail",
                                {"media": media, "bleed": bleed}, "MediaBox covers the full bleed",
                                "BleedBox must not exceed the actual page"))
        bc = bleed_content(pdf, boxes, b)
        checks.append(check("pdf.bleedcontent", "Final PDF bleed content",
                            bc.get("status", "pending"), bc,
                            "No pure white within 3 mm on all four sides; PENDING when there is no bleed area",
                            "Page boxes alone are not enough; check the actual ink"))

    # Output intent / embedded CMYK profile / PDF/X identification
    contract = (job.get("pdfExport") or {}).get("contract")
    oi = st.get("outputIntent", 0)
    chain = pdfprobe.output_intents(pdf)
    cond = chain[0]["condition"] if chain else None
    dest_profile = chain[0]["destProfile"] if chain else None
    pdfx_required = _pdfx_target(job, spec)
    target_icc = spec["print"]["iccProfile"]
    target_sha = hashlib.sha256(open(target_icc, "rb").read()).hexdigest()
    profiles = info.get("profiles", [])
    # /DefaultCMYK must be a colour space array [/ICCBased <stream>]; a
    # bare stream reference is not a valid colour space and is ignored.
    dcm_ok = False
    try:
        from pypdf import PdfReader as _ICCReader
        from pypdf.generic import ArrayObject as _ICCArray
        _p = _ICCReader(pdf).pages[0]
        _cs = _p["/Resources"]["/ColorSpace"].get_object()
        _d = _cs.get("/DefaultCMYK")
        _d = _d.get_object() if hasattr(_d, "get_object") else _d
        dcm_ok = (isinstance(_d, _ICCArray) and len(_d) == 2
                  and str(_d[0]) == "/ICCBased")
        # The referenced stream itself must be the target ICC, not just
        # some other CMYK profile that happens to be embedded elsewhere.
        if dcm_ok:
            _stream = _d[1].get_object()
            _icc_data = _stream.get_data()
            dcm_ok = (hashlib.sha256(_icc_data).hexdigest() == target_sha
                      and int(_stream.get("/N", 0)) == 4
                      and len(_icc_data) > 132
                      and _icc_data[16:20] == b"CMYK"
                      and _icc_data[36:40] == b"acsp")
    except Exception:  # noqa: BLE001
        dcm_ok = False
    headers = raw[:8].decode("latin1")
    hdr_ok = headers.startswith("%PDF-1.6")
    intent_ok = (len(chain) == 1 and bool(dest_profile)
                 and dest_profile.get("sha256") == target_sha
                 and dest_profile.get("isCMYK") and not dest_profile.get("isRGB"))
    checks.append(check("pdf.outputintent", "GTS_PDFX OutputIntent + target ICC",
                        "pass" if (intent_ok and dcm_ok and hdr_ok) else "fail",
                        {"count": len(chain), "condition": cond,
                         "header": headers.strip(),
                         "defaultCMYKValid": dcm_ok, "destProfile": dest_profile,
                         "iccSha256": target_sha,
                         "profiles": [{k: x.get(k) for k in ("description", "space", "sha256")}
                                      for x in profiles]},
                        "Exactly 1 GTS_PDFX; DestOutputProfile = the spec delivery ICC; /DefaultCMYK uses the same ICC",
                        "Both the ICC hash and the structure must match; a label cannot fake a conversion"))

    # PDF/X-4 structural conformance: internal Preflight equivalent over
    # file facts. It does not replace Acrobat Preflight or the printer's own
    # preflight, and it never upgrades a plain PDF by relabelling.
    subs = {}
    try:
        from pypdf import PdfReader as _XReader
        _xr = _XReader(pdf)
        _mi = {str(k): str(v) for k, v in (_xr.metadata or {}).items()}
        subs["infoGtsPdfxVersion"] = _mi.get("/GTS_PDFXVersion")
        subs["infoTrapped"] = (str(_mi.get("/Trapped")).lstrip("/")
                               if _mi.get("/Trapped") is not None else None)
        subs["infoCreationDate"] = bool(_mi.get("/CreationDate"))
        _xmp_ref = _xr.trailer["/Root"].get("/Metadata")
        if _xmp_ref is not None:
            _xmp_ref = _xmp_ref.get_object()
        _xmp_txt = (_xmp_ref.get_data().decode("utf-8", "replace")
                    if _xmp_ref is not None else "")
        subs["xmpPresent"] = bool(_xmp_txt)
        _mg = re.search(r"pdfxid:GTS_PDFXVersion>([^<]+)<", _xmp_txt)
        subs["xmpGtsPdfxVersion"] = _mg.group(1) if _mg else None
        subs["xmpTitle"] = bool(re.search(r"dc:title", _xmp_txt))
        _blob = raw
        try:
            _blob = _decoded_blob(pdf, raw)
        except Exception:  # noqa: BLE001
            pass
        subs["noPS"] = not re.search(rb"/Subtype\s*/PS\b", _blob)
        subs["noActions"] = (not re.search(rb"/AA\b", _blob)
                             and not re.search(rb"/JavaScript\b", _blob)
                             and not re.search(rb"/OpenAction\b", _blob)
                             and not re.search(
                                 rb"/S\s*/(?:Launch|URI|JavaScript|SubmitForm|GoToR|RichMedia|Sound)\b",
                                 _blob))
        subs["noEncryption"] = not _xr.is_encrypted
        subs["noDeviceRGB"] = rgb_ops == 0
    except Exception as _exc:  # noqa: BLE001
        subs["error"] = str(_exc)
    if pdfx_required in (None, "printer-defined"):
        pdfx_status = "pending"
    else:
        pdfx_ok = (subs.get("infoGtsPdfxVersion") == pdfx_required
                   and subs.get("infoTrapped") in ("False", "True")
                   and subs.get("infoCreationDate")
                   and subs.get("xmpPresent")
                   and subs.get("xmpGtsPdfxVersion") == pdfx_required
                   and subs.get("xmpTitle")
                   and subs.get("noPS") and subs.get("noActions")
                   and subs.get("noEncryption") and subs.get("noDeviceRGB"))
        pdfx_status = "pass" if pdfx_ok else "fail"
    checks.append(check("pdf.pdfx", "PDF/X-4 structure self-check", pdfx_status,
                        {"required": pdfx_required, "subs": subs},
                        "Info and XMP version ids agree, Trapped set, no /PS, no Action/JS, no encryption, no RGB",
                        "Internal structure self-check; it does not replace Acrobat Preflight or the print shop check"))

    # Colour operators in page content
    checks.append(check("pdf.rgb", "RGB colour operators in page content",
                        "pass" if rgb_ops == 0 else "fail",
                        rgb_ops, 0,
                        "CMYK operators=%d; /DeviceRGB byte count=%d (may come from a thumbnail)" % (
                            cmyk_ops, st.get("deviceRGB", 0))))
    checks.append(check("pdf.cmyk", "CMYK colour operators in page content",
                        "pass" if cmyk_ops > 0 else "fail", cmyk_ops, "> 0", ""))
    try:
        from pypdf import PdfReader as _PageReader
        tree_pages = len(_PageReader(pdf).pages)
    except Exception:  # noqa: BLE001
        tree_pages = info.get("pages")
    checks.append(check("pdf.pages", "Page count", "pass" if tree_pages == 1 else "fail",
                        {"pageTree": tree_pages, "pageObjects": info.get("pages")}, 1,
                        "Counted from the page tree; orphan page objects kept for merging do not count"))

    # Strict parse: dangling indirect references (e.g. /Resources copied from
    # another writer) are silently tolerated by viewers but must fail here.
    try:
        strict = subprocess.run([GS, "-q", "-dPDFSTOPONERROR", "-dBATCH", "-dNOPAUSE",
                                 "-dSAFER", "-sDEVICE=nullpage", pdf],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        err_tail = strict.stderr.decode("latin1", "replace").strip().splitlines()[-3:]
        checks.append(check("pdf.strict", "Strict parse (no dangling references)",
                            "pass" if strict.returncode == 0 else "fail",
                            {"returncode": strict.returncode, "stderr": err_tail},
                            0, "Ghostscript -dPDFSTOPONERROR; resources must parse"))
    except Exception as exc:  # noqa: BLE001
        checks.append(check("pdf.strict", "Strict parse (no dangling references)", "pending",
                            str(exc), 0, "Ghostscript not available"))

    # Structural balance of the page content stream. An unbalanced Q (or a
    # stray EMC) is tolerated by viewers but breaks strict RIPs, so it is a
    # hard FAIL here.
    try:
        from pypdf import PdfReader as _PdfReader
        _contents = _PdfReader(pdf).pages[0].get_contents()
        _stream = _contents.get_data() if _contents is not None else b""
        _toks = _stream.split()
        _n = lambda t: sum(1 for x in _toks if x == t)
        _q, _qc = _n(b"q"), _n(b"Q")
        _bmc, _bdc, _emc = _n(b"BMC"), _n(b"BDC"), _n(b"EMC")
        _balanced = (_q == _qc and _bmc + _bdc == _emc)
        checks.append(check("pdf.stream", "Content stream q/Q and marked block balance",
                            "pass" if _balanced else "fail",
                            {"q": _q, "Q": _qc, "BMC": _bmc, "BDC": _bdc, "EMC": _emc},
                            "q == Q and BMC + BDC == EMC",
                            "Q underflow or a stray EMC makes a strict RIP fail"))
    except Exception as _exc:
        checks.append(check("pdf.stream", "Content stream q/Q and marked block balance", "pending",
                            str(_exc), "q == Q and BMC + BDC == EMC", "pypdf not available"))

    # Optional content: if the marks stream uses /OC, the catalog must carry a
    # valid /OCProperties whose OCGs are real and on by default.
    try:
        from pypdf import PdfReader as _OCReader
        _r = _OCReader(pdf)
        _pg = _r.pages[0]
        _contents = _pg.get_contents()
        _content = _contents.get_data() if _contents is not None else b""
        if not _content:
            _content = raw
        _uses_oc = b"/OC /" in _content or b"/OC\n/" in _content
        _root = _r.trailer["/Root"]
        _ocp = _root.get("/OCProperties")
        if not _uses_oc:
            _oc_status, _oc_value = "pass", "No optional content used"
        elif _ocp is None:
            _oc_status, _oc_value = "fail", "Content uses /OC but the Catalog has no OCProperties"
        else:
            _ocp = _ocp.get_object()
            _refs = [_x.get_object() for _x in _ocp.get("/OCGs", [])]
            _bad = [str(x.get("/Name")) for x in _refs
                    if str(x.get("/Type")) != "/OCG" or not x.get("/Name")]
            _d = (_ocp.get("/D") or {}).get_object() if _ocp.get("/D") else {}
            _on_refs = _d.get("/ON", []) if _d else []
            _on_ids = [id(x.get_object()) for x in _on_refs]
            _all_ids = [id(x) for x in _refs]
            _on = len(_on_ids)
            _valid = (bool(_refs) and not _bad
                      and len(_on_ids) == len(set(_on_ids)) == len(_refs)
                      and set(_on_ids) == set(_all_ids)
                      and not _d.get("/OFF")
                      and str(_d.get("/BaseState") or "/ON") == "/ON")
            _oc_status = "pass" if _valid else "fail"
            _oc_value = {"ocgs": len(_refs), "on": _on, "bad": _bad}
        checks.append(check("pdf.ocproperties", "Optional content configuration", _oc_status, _oc_value,
                            "OCProperties present; every OCG has a Name and is on by default",
                            "Some RIPs drop output when an /OC layer has no configuration"))
    except Exception as _exc:  # noqa: BLE001
        checks.append(check("pdf.ocproperties", "Optional content configuration", "pending", str(_exc),
                            "OCProperties valid", "pypdf not available"))

    # ---------- Mark geometry (jp-double) ----------
    try:
        import marks as markslib  # noqa: E402
        from pypdf import PdfReader as _MarkReader
        _mr = _MarkReader(pdf)
        _mp = _mr.pages[0]
        _mc = _mp.get_contents()
        _mdata = _mc.get_data() if _mc is not None else b""
        _i = _mdata.find(b"/CSAll CS")
        if _i < 0:
            _i = _mdata.find(b"/CSAll cs")
        _block = _mdata[_i:_mdata.find(b"\nQ", _i)] if _i >= 0 else b""
        _trim = [float(x) for x in boxes.get("TrimBox", [])]
        _mcfg = markslib.config(load(os.path.join(ROOT, "config", "marks.json")) or {})
        _expected = markslib.segments(_trim, _mcfg)
        _found = [(float(a), float(b), float(c), float(d))
                  for a, b, c, d in re.findall(
                      rb"([-\d.]+) ([-\d.]+) m\s+([-\d.]+) ([-\d.]+) l", _block)]

        def _norm(seg):
            (ax, ay), (bx, by) = seg[0], seg[1]
            if (ax, ay) > (bx, by):
                ax, ay, bx, by = bx, by, ax, ay
            return (round(ax, 1), round(ay, 1), round(bx, 1), round(by, 1))

        _exp = sorted(_norm(x) for x in _expected)
        _got = sorted(_norm(((a, b), (c, d))) for a, b, c, d in _found)
        _geom_ok = _exp == _got
        checks.append(check("pdf.marks.geometry", "Japanese double-line corner marks + centre cross",
                            "pass" if _geom_ok else "fail",
                            {"expected": len(_exp), "found": len(_got),
                             "missing": [s for s in _exp if s not in _got][:6],
                             "extra": [s for s in _got if s not in _exp][:6]},
                            "All 24 segments match the print-spec marks one by one",
                            "FAIL when the deviation exceeds 0.1 pt"))
        checks.append(check("pdf.marks.count", "Mark segment count",
                            "pass" if len(_found) == 24 else "fail",
                            len(_found), 24, "4 corners x 4 segments + 4 crosses x 2 segments"))
        _color_ok = (b"/CSAll CS" in _block and b"1 SCN" in _block
                     and b"/CSAll cs" not in _block and b"1 scn" not in _block
                     and not re.search(rb"(?:^|\n)[\d.]+ [\d.]+ [\d.]+ [\d.]+ k", _block)
                     and not re.search(rb"(?:^|\n)[\d.]+ [\d.]+ [\d.]+ rg", _block))
        checks.append(check("pdf.marks.color", "Marks use the registration colour",
                            "pass" if _color_ok else "fail",
                            {"separationAll": b"/CSAll CS" in _block,
                             "tint100": b"1 SCN" in _block,
                             "strokingSpace": b"/CSAll cs" not in _block},
                            "Stroking = /Separation /All 100%",
                            "K100 prints on one plate only and cannot register; non-stroking cs/scn does not count"))
        _wm = re.search(rb"([\d.]+) w", _block)
        _width = float(_wm.group(1)) if _wm else None
        checks.append(check("pdf.marks.width", "Mark line width",
                            "pass" if _width is not None and abs(_width - 0.3) <= 0.05 else "fail",
                            _width, "0.3 pt ±0.05", ""))

        # Filled rectangles entirely outside TrimBox are colour bars.
        _bars = []
        _path = None
        for _args, _op in _mp.get_contents().operations:
            if _op == b"re" and len(_args) == 4:
                _x, _y, _w, _h = [float(v) for v in _args]
                _x0, _x1 = min(_x, _x + _w), max(_x, _x + _w)
                _y0, _y1 = min(_y, _y + _h), max(_y, _y + _h)
                _path = [_x0, _y0, _x1, _y1]
            elif _op in (b"m", b"l") and len(_args) == 2:
                _x, _y = [float(v) for v in _args]
                if _path is None:
                    _path = [_x, _y, _x, _y]
                else:
                    _path = [min(_path[0], _x), min(_path[1], _y),
                             max(_path[2], _x), max(_path[3], _y)]
            elif _op in (b"f", b"f*", b"B", b"B*", b"b", b"b*"):
                if _path and len(_trim) == 4:
                    _outside = (_path[2] <= _trim[0] + 0.1 or _path[0] >= _trim[2] - 0.1
                                or _path[3] <= _trim[1] + 0.1 or _path[1] >= _trim[3] - 0.1)
                    _inside_media = (_path[0] >= -0.1 and _path[1] >= -0.1
                                     and _path[2] <= float(boxes["MediaBox"][2]) + 0.1
                                     and _path[3] <= float(boxes["MediaBox"][3]) + 0.1)
                    if _outside and _inside_media:
                        _bars.append([round(v, 1) for v in _path])
                _path = None
            elif _op in (b"n", b"S", b"s"):
                _path = None
        checks.append(check("pdf.marks.bars", "No colour bar outside the TrimBox",
                            "pass" if not _bars else "fail",
                            _bars[:6], 0, "The mark area allows lines only, not solid colour patches"))
    except Exception as _exc:  # noqa: BLE001
        checks.append(check("pdf.marks.geometry", "Japanese double-line corner marks + centre cross",
                            "pending", str(_exc), "parseable", "marks module or content stream not available"))

    # RGB images are not caught by content-stream colour operators.
    images = pdfprobe.image_colours(pdf)
    rgb_images = [x["object"] for x in images if x["kind"] == "rgb"]
    checks.append(check("pdf.imagesRGB", "RGB left in page images",
                        "pass" if not rgb_images else "fail",
                        {"rgb": rgb_images[:10], "all": images}, 0,
                        "Zero RGB in the content stream does not mean the images are CMYK"))
    unknown_images = [x["object"] for x in images if x["kind"] == "unknown"]
    checks.append(check("pdf.imagesUnknown", "Page image colour spaces parse",
                        "pending" if unknown_images else "pass",
                        unknown_images[:10], 0,
                        "Manual confirmation is required when a resource name such as /CS0 does not parse"))

    # Fonts: every non-Type3 font must be embedded (FontFile/FontFile2/FontFile3).
    # Type3 entries with no CharProc painting ops are outline shells, not
    # external fonts; pdf.visual still covers what they render.
    font_items = []
    try:
        from pypdf import PdfReader as _FontReader
        _p = _FontReader(pdf).pages[0]
        _queue = [(_p.get("/Resources") or {})]
        _seen_res = set()
        _seen_font = set()
        while _queue:
            _res = _queue.pop()
            _res = _res.get_object() if hasattr(_res, "get_object") else _res
            if not _res or id(_res) in _seen_res:
                continue
            _seen_res.add(id(_res))
            _fonts = _res.get("/Font") or {}
            if hasattr(_fonts, "get_object"):
                _fonts = _fonts.get_object()
            for _name, _ref in _fonts.items():
                _f = _ref.get_object()
                if id(_f) in _seen_font:
                    continue
                _seen_font.add(id(_f))
                _sub = str(_f.get("/Subtype"))
                _base = str(_f.get("/BaseFont") or "")
                embedded = None
                if _sub != "/Type3":
                    _desc = _f
                    if _f.get("/DescendantFonts"):
                        _desc = _f["/DescendantFonts"][0].get_object()
                    _fd = _desc.get("/FontDescriptor")
                    if _fd is not None:
                        _fd = _fd.get_object()
                        for _fk in ("/FontFile", "/FontFile2", "/FontFile3"):
                            if _fk in _fd:
                                try:
                                    embedded = len(_fd[_fk].get_object().get_data()) > 0
                                except Exception:  # noqa: BLE001
                                    embedded = False
                                break
                font_items.append({"name": str(_name), "subtype": _sub,
                                   "baseFont": _base, "embedded": embedded})
            # Form XObjects carry their own /Resources, including fonts.
            _xobj = _res.get("/XObject") or {}
            if hasattr(_xobj, "get_object"):
                _xobj = _xobj.get_object()
            for _xname, _xref in _xobj.items():
                _xo = _xref.get_object()
                if str(_xo.get("/Subtype")) == "/Form" and _xo.get("/Resources"):
                    _queue.append(_xo.get("/Resources"))
        _live = [x for x in font_items if x["subtype"] != "/Type3"]
        _missing = [x["name"] for x in _live if not x["embedded"]]
        _type3 = [x for x in font_items if x["subtype"] == "/Type3"]
        type3_paint = pdfprobe.type3_painting_glyphs(pdf) if _type3 else 0
        font_status = "fail" if (_missing or type3_paint > 0) else "pass"
        font_value = {"fonts": font_items, "type3Count": len(_type3),
                      "type3PaintOps": type3_paint, "unembedded": _missing,
                      "rule": "no unembedded font; Type3 shells must not paint"}
    except Exception as exc:  # noqa: BLE001
        font_status = "pending"
        font_value = str(exc)
    checks.append(check("pdf.fonts", "Font embedding", font_status, font_value,
                        "Every non-Type3 font is embedded; Type3 may be a non-painting shell",
                        "Outlined text is not a hard requirement; embedding is what counts"))

    # Separations / overprint
    sep = st.get("separationNames", [])
    if (layout == "marks" and st.get("allSeparation")
            and st.get("deviceN", 0) == 0 and sep and all(x == "All" for x in sep)):
        sep_status = "pass"
        sep_note = "PDF/X-4 allows /Separation /All and /None; only /All is present (registration marks, outside the TrimBox)"
    else:
        sep_status = "fail" if (st.get("deviceN", 0) > 0 or st.get("allSeparation")) else (
            "pending" if sep else "pass")
        sep_note = "Named spot colours need print shop confirmation; /All is for marks only"
    checks.append(check("pdf.separations", "Spot colours / separations",
                        sep_status,
                        {"names": sep, "deviceN": st.get("deviceN", 0),
                         "allSeparation": st.get("allSeparation")},
                        "No spot colour in the artwork; mark /All is allowed outside only",
                        sep_note))
    over = st.get("overprintTrue", 0) + st.get("opmTrue", 0)
    checks.append(check("pdf.overprint", "Overprint / overprint mode",
                        "pending" if over else "pass", over, 0,
                        "A non-zero count must be confirmed item by item in Acrobat Output Preview; it cannot be accepted automatically"))

    # TAC
    vec = info.get("vectorMaxTac") or {}
    media = boxes.get("MediaBox")
    tac_crop = boxes.get("BleedBox") if layout == "marks" else None
    if tac_crop:
        # Registration marks sit on the bleed line. Measure ink inside it.
        _inset = 0.9  # pt
        tac_crop = [tac_crop[0] + _inset, tac_crop[1] + _inset,
                    tac_crop[2] - _inset, tac_crop[3] - _inset]
    tac150 = render_tac(pdf, 150, media_box=media, crop_box=tac_crop)
    tac300 = render_tac(pdf, 300, vec.get("cmyk"), media_box=media, crop_box=tac_crop)
    vec_tac = vec.get("tac", 0) if tac300.get("vectorVisible") else 0
    max_tac = max(tac150["maxTacPercent"], tac300["maxTacPercent"], vec_tac)
    limit = job.get("tacLimitPercent")
    if limit is None:
        limit = spec["print"].get("tacLimitPercent")
    if limit is None:
        tac_status = "pending"
    elif max_tac > float(limit):
        tac_status = "fail"
    else:
        tac_status = "pass"
    checks.append(check("pdf.tac", "Total area coverage (TAC)", tac_status,
                        {"raster150": tac150, "raster300": tac300,
                         "vector": vec, "vectorVisible": tac300.get("vectorVisible"),
                         "limitPercent": limit},
                        "≤ %s (spec.print.tacLimitPercent); above the limit use a correct conversion or ink limiting, never clip or round"
                        % (limit if limit is not None else "TBD"),
                        "The vector value catches small text and hairlines that the raster misses"))

    # Visual comparison with the approved AI
    job_dir = os.path.dirname(pdf)
    if os.path.basename(pdf) == "cmyk.pdf":
        job_dir = os.path.dirname(job_dir)
    ai = os.path.join(job_dir, job["output"]["ai"])
    ai_sha = None
    if not trim:
        checks.append(check("pdf.visual", "Final PDF vs approved AI soft proof",
                            "pending", "TrimBox missing; cannot determine the crop area",
                            "TrimBox present", ""))
    elif os.path.exists(ai):
        ai_sha = hashlib.sha256(open(ai, "rb").read()).hexdigest()
        try:
            media = boxes.get("MediaBox")
            if layout == "marks":
                visual = visual_compare(pdf, ai, dpi=150, media_box=media, crop_box=trim)
            else:
                crop_mm = 0.0
                if media and trim and len(media) == 4 and pt2mm(media[2] - media[0]) > tw + 0.5:
                    crop_mm = b
                visual = visual_compare(pdf, ai, dpi=150, crop_mm=crop_mm)
            ink_bad = visual.get("inkMismatchPercent", 0) > 0.02
            strong_bad = visual.get("strongBlobPercent", 0) > 0.0005
            visual["status"] = "pass" if (visual["median"] <= 0.5
                                          and visual["over5Percent"] <= 0.05
                                          and not ink_bad and not strong_bad) else "fail"
            checks.append(check("pdf.visual", "Final PDF vs approved AI soft proof",
                                visual["status"], visual,
                                "median ≤ 0.5, over5 ≤ 0.05%, ink difference ≤ 0.02%, solid difference blob ≤ 0.0005%",
                                "Software ICC simulation; beyond flat-colour ΔE it also checks the whole-image ink mask difference"))
        except Exception as exc:  # noqa: BLE001
            checks.append(check("pdf.visual", "Final PDF vs approved AI soft proof",
                                "pending", str(exc), "comparable", ""))
    else:
        checks.append(check("pdf.visual", "Final PDF vs approved AI soft proof",
                            "pending", "AI not found", "AI present", ""))

    # attention is a non-pass result; never let it fall through to PASS.
    fails = [c for c in checks if c["status"] in ("fail", "attention")]
    pend = [c for c in checks if c["status"] == "pending"]
    overall = "fail" if fails else ("pending" if pend else "pass")
    return {
        "ok": True,
        "selftest": bool(selftest),
        "jobId": job.get("jobId"),
        "pdf": pdf,
        "sha256": info["sha256"],
        "aiSha256": ai_sha,
        "acceptanceHash": acceptance.digest(job, spec),
        "overall": overall,
        "counts": {"pass": sum(1 for c in checks if c["status"] == "pass"),
                   "fail": len(fails), "pending": len(pend)},
        "checks": checks,
        "limits": ["Checks file facts only. It does not certify full PDF/X compliance.",
                   "Printer output condition, paper and contract proof remain external."],
    }


def main():
    args = sys.argv[1:]
    if not args:
        print("usage: 08-gate3.py <job-id> [--selftest]", file=sys.stderr)
        return 64
    job_id = args[0]
    selftest = "--selftest" in args
    job_dir = os.path.join(ROOT, "jobs", job_id)
    job = load(os.path.join(job_dir, "job.json"))
    spec = load(os.path.join(ROOT, "config", "print-spec.json"))
    global GS
    _gs = ((spec or {}).get("ghostscript") or {}).get("binary") or "gs"
    GS = shutil.which(_gs) or _gs
    if not job:
        print("job.json not found", file=sys.stderr)
        return 66
    pdf = os.path.join(job_dir, "work", "cmyk.pdf") if selftest else os.path.join(job_dir, job["output"]["pdf"])
    if not os.path.exists(pdf):
        result = {
            "ok": True, "selftest": selftest, "jobId": job_id,
            "pdf": pdf, "overall": "pending",
            "counts": {"pass": 0, "fail": 0, "pending": 1},
            "checks": [check("pdf.exists", "Final PDF exists", "pending", "missing",
                             "Export manually from Illustrator, then rerun Gate 3", "")],
            "limits": ["Gate 3 cannot run until the delivery PDF exists."],
        }
        out = "gate3-selftest.json" if selftest else "gate3.json"
        with open(os.path.join(job_dir, out), "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
        print(json.dumps({"overall": "pending", "pdf": pdf}, ensure_ascii=False))
        return 0
    result = analyse(pdf, job, spec, selftest=selftest)
    if selftest:
        expected = {
            "pdf.trimbox": "fail",
            "pdf.bleedbox": "fail",
            "pdf.outputintent": "fail",
            "pdf.fonts": "pass",
            "pdf.pdfx": "fail",
            "pdf.rgb": "pass",
            "pdf.cmyk": "pass",
            "pdf.imagesRGB": "pass",
        }
        actual = {c["id"]: c["status"] for c in result["checks"]}
        mismatches = {k: {"expected": v, "actual": actual.get(k)}
                      for k, v in expected.items() if actual.get(k) != v}
        result["selftestExpected"] = expected
        result["selftestMismatches"] = mismatches
        result["selftestPass"] = not mismatches
        out = "gate3-selftest.json"
    else:
        out = "gate3.json"
    with open(os.path.join(job_dir, out), "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    print(json.dumps({"overall": result["overall"], "counts": result["counts"],
                      "selftest": result.get("selftestPass")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
