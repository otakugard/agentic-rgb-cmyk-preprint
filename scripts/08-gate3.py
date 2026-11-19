#!/usr/bin/env python3
"""08-gate3.py <job-id> [--selftest]

Gate 3: inspect the final delivery PDF after the human exports it.
It does not create the PDF and it does not claim full PDF/X compliance;
it checks the facts this pipeline can verify from the file.

--selftest runs the same checks on work/cmyk.pdf (which is not a delivery
PDF) and writes gate3-selftest.json to prove that missing OutputIntent /
PDF-X are detected. Page boxes depend on the source and are not asserted.
"""

import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "lib"))
import acceptance  # noqa: E402
import color  # noqa: E402
import contract as colorcontract  # noqa: E402
import pdfpaint  # noqa: E402
import pdfprobe  # noqa: E402
import raster  # noqa: E402

GS = shutil.which("gs") or "/opt/homebrew/bin/gs"
MUTOOL = shutil.which("mutool")


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


def mm2pt(v):
    return v * 72 / 25.4


def trim_mm(job, spec):
    if job.get("trim", {}).get("preset"):
        w, h = spec["trimPresetsMm"][job["trim"]["preset"]]
        return float(w), float(h)
    return float(job["trim"]["w"]), float(job["trim"]["h"])


def check(cid, label, status, value, expected, evidence=""):
    return {"id": cid, "label": label, "status": status,
            "value": value, "expected": expected, "evidence": evidence}


def _sha_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _page_resources(page):
    node = page
    seen = set()
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        res = node.get("/Resources")
        if res is not None:
            return res.get_object()
        parent = node.get("/Parent")
        node = parent.get_object() if parent is not None else None
    return {}


def _cs_dict(resources):
    cs = (resources or {}).get("/ColorSpace")
    if cs is None:
        return {}
    cs = cs.get_object()
    return {str(k).lstrip("/"): v for k, v in cs.items()}


def _stream_data(obj):
    """Decode a stream object robustly.

    pypdf can return an empty DecodedStreamObject from .get_object() on a
    /Contents entry while the entry itself still holds the bytes, so try the
    original object first.
    """
    candidates = [obj]
    if hasattr(obj, "get_object"):
        try:
            candidates.append(obj.get_object())
        except Exception:  # noqa: BLE001
            pass
    for cand in candidates:
        try:
            data = cand.get_data()
            if data:
                return data
        except Exception:  # noqa: BLE001
            continue
    return b""


def _page_content_data(page):
    contents = page.get("/Contents")
    if contents is None:
        return b""
    if isinstance(contents, list) or str(type(contents)).endswith("ArrayObject"):
        return b"\n".join(_stream_data(x) for x in contents)
    return _stream_data(contents)


def _classify_cs(value):
    """Classify a colour-space value without assuming a library default."""
    from pypdf.generic import ArrayObject, NameObject
    obj = value.get_object() if hasattr(value, "get_object") else value
    if isinstance(obj, NameObject):
        name = str(obj)
        if name in ("/DeviceCMYK",):
            return {"kind": "cmyk", "source": "DeviceCMYK"}
        if name in ("/DeviceRGB", "/CalRGB"):
            return {"kind": "rgb", "source": name.lstrip("/")}
        if name in ("/DeviceGray", "/CalGray"):
            return {"kind": "gray", "source": name.lstrip("/")}
        if name == "/Pattern":
            return {"kind": "pattern", "source": "Pattern"}
        return {"kind": "named", "source": name.lstrip("/")}
    if isinstance(obj, ArrayObject) and len(obj) >= 1:
        head = str(obj[0])
        if head == "/ICCBased" and len(obj) >= 2:
            stream = obj[1].get_object()
            data = stream.get_data()
            n = int(stream.get("/N", 0))
            kind = "cmyk" if n == 4 else "rgb" if n == 3 else "other"
            return {"kind": kind, "source": "ICCBased", "n": n,
                    "sha256": _sha_bytes(data),
                    "description": str(stream.get("/Desc") or "")[:120]}
        if head == "/Separation" and len(obj) >= 2:
            return {"kind": "separation", "source": "Separation",
                    "name": str(obj[1]).lstrip("/")}
        if head == "/DeviceN":
            return {"kind": "devicen", "source": "DeviceN"}
        if head == "/Indexed" and len(obj) >= 2:
            return {"kind": "indexed", "source": "Indexed",
                    "base": _classify_cs(obj[1])}
        if head in ("/CalRGB", "/DeviceRGB"):
            return {"kind": "rgb", "source": head.lstrip("/")}
        if head in ("/CalGray", "/DeviceGray"):
            return {"kind": "gray", "source": head.lstrip("/")}
    return {"kind": "unknown", "source": str(obj)[:80]}


def defaultcmyk_check(pdf, target_sha):
    """Dedicated /DefaultCMYK regression gate (array form + ICC identity)."""
    out = {"count": 0, "arrayForm": False, "pageLevel": False,
           "streamSha256": None, "matchesTarget": False, "n": None,
           "space": None, "formsWithOwnDefaultCMYK": [],
           "headerIntent": None}
    try:
        from pypdf import PdfReader as _R
        from pypdf.generic import ArrayObject
        reader = _R(pdf)
        page = reader.pages[0]
        cs = _cs_dict(_page_resources(page))
        dcm = cs.get("DefaultCMYK")
        if dcm is not None:
            out["count"] = 1
            out["pageLevel"] = True
            dcm = dcm.get_object()
            out["arrayForm"] = isinstance(dcm, ArrayObject) and len(dcm) == 2 and str(dcm[0]) == "/ICCBased"
            if out["arrayForm"]:
                stream = dcm[1].get_object()
                data = stream.get_data()
                out["streamSha256"] = _sha_bytes(data)
                out["matchesTarget"] = out["streamSha256"] == target_sha
                out["n"] = int(stream.get("/N", 0))
                out["space"] = data[16:20].decode("latin1", "replace") if len(data) >= 40 else None
                out["headerIntent"] = int.from_bytes(data[64:68], "big") if len(data) >= 68 else None
        # Forms may declare their own DefaultCMYK; that would split the ink space.
        seen = set()

        def walk(res):
            xo = (res or {}).get("/XObject")
            xo = xo.get_object() if hasattr(xo, "get_object") else (xo or {})
            for name, ref in xo.items():
                obj = ref.get_object()
                if id(obj) in seen:
                    continue
                seen.add(id(obj))
                if str(obj.get("/Subtype")) != "/Form":
                    continue
                fres = obj.get("/Resources")
                if fres is not None:
                    fres = fres.get_object()
                    if "DefaultCMYK" in _cs_dict(fres):
                        out["formsWithOwnDefaultCMYK"].append(str(name))
                    walk(fres)
        walk(_page_resources(page))
        out["ok"] = bool(out["count"] == 1 and out["arrayForm"]
                         and out["matchesTarget"] and out["n"] == 4
                         and out["space"] == "CMYK"
                         and not out["formsWithOwnDefaultCMYK"])
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
        out["ok"] = False
    return out


def defaultcmyk_absence_check(pdf):
    """PDF/X-4 process artwork must not re-declare DeviceCMYK as ICCBased CMYK.

    PDF/X-4 does not require a /DefaultCMYK. An ICCBased CMYK default space
    identical to the OutputIntent re-maps bare k at the reading stage
    (RCA 2026-11-16). New delivery jobs use one OutputIntent and no default
    CMYK remap in the page or any reachable Form.
    """
    out = {"status": "pass", "pageDefaultCMYK": False,
           "formDefaultCMYK": [], "count": 0,
           "policy": "DeviceCMYK under one CMYK OutputIntent; no ICCBased /DefaultCMYK"}
    try:
        from pypdf import PdfReader as _R
        reader = _R(pdf)
        page = reader.pages[0]
        page_cs = _cs_dict(_page_resources(page))
        if "DefaultCMYK" in page_cs:
            out["pageDefaultCMYK"] = True
            out["count"] += 1
        seen = set()

        def walk(res, label):
            xo = (res or {}).get("/XObject")
            xo = xo.get_object() if hasattr(xo, "get_object") else (xo or {})
            for name, ref in xo.items():
                obj = ref.get_object()
                if id(obj) in seen:
                    continue
                seen.add(id(obj))
                if str(obj.get("/Subtype")) != "/Form":
                    continue
                fres = obj.get("/Resources")
                if fres is not None:
                    fres = fres.get_object()
                    if "DefaultCMYK" in _cs_dict(fres):
                        out["formDefaultCMYK"].append("%s/%s" % (label, name))
                        out["count"] += 1
                    walk(fres, "%s/%s" % (label, name))
                else:
                    walk(res, "%s/%s" % (label, name))

        walk(_page_resources(page), "page")
        out["status"] = "pass" if out["count"] == 0 else "fail"
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
        out["status"] = "pending"
    return out


def inheritedcs_check(pdf, target_sha):
    """Colour spaces used by page/form content, including forms without /Resources."""
    out = {"scopes": [], "used": [], "unresolved": [], "rgbSpaces": [],
           "wrongIcc": [], "iccCmykSpaces": [], "spotNames": [], "inheritedForms": 0,
           "deviceCMYKInInheritedForms": 0, "pageDefaultCMYK": False,
           "error": None}
    try:
        from pypdf import PdfReader as _R
        reader = _R(pdf)
        page = reader.pages[0]
        page_res = _page_resources(page)
        out["pageDefaultCMYK"] = "DefaultCMYK" in _cs_dict(page_res)
        scopes = []
        seen = set()

        def add_scope(label, resources, inherited, content, has_own_resources):
            scopes.append({"scope": label, "resources": resources,
                           "inherited": inherited,
                           "hasOwnResources": has_own_resources,
                           "content": content})

        def walk(resources, label, inherited):
            xo = (resources or {}).get("/XObject")
            xo = xo.get_object() if hasattr(xo, "get_object") else (xo or {})
            for name, ref in xo.items():
                obj = ref.get_object()
                if id(obj) in seen:
                    continue
                seen.add(id(obj))
                if str(obj.get("/Subtype")) != "/Form":
                    continue
                has_res = "/Resources" in obj
                fres = obj["/Resources"].get_object() if has_res else resources
                local_cs = _cs_dict(fres)
                # An empty /Resources dict has no usable local colour space:
                # its DeviceCMYK numbers are read in the page/device space.
                relies_on_page = inherited or (not has_res) or (not local_cs)
                try:
                    content = obj.get_data()
                except Exception:  # noqa: BLE001
                    content = b""
                add_scope("%s/%s" % (label, name), fres, relies_on_page,
                          content, has_res)
                walk(fres, "%s/%s" % (label, name), relies_on_page)

        try:
            page_content = _page_content_data(page)
        except Exception:  # noqa: BLE001
            page_content = b""
        add_scope("page", page_res, False, page_content, True)
        walk(page_res, "page", False)

        import re as _re
        use_rx = _re.compile(rb"/([A-Za-z0-9#_.+-]+)\s+(?:cs|CS)\b")
        k_rx = _re.compile(rb"(-?(?:\d+(?:\.\d*)?|\.\d+))\s+(-?(?:\d+(?:\.\d*)?|\.\d+))"
                           rb"\s+(-?(?:\d+(?:\.\d*)?|\.\d+))\s+(-?(?:\d+(?:\.\d*)?|\.\d+))\s+[kK]\b")
        for s in scopes:
            entries = _cs_dict(s["resources"])
            declared = {}
            for name, value in entries.items():
                if name == "DefaultCMYK":
                    continue
                declared[name] = _classify_cs(value)
            used_names = sorted({m.group(1).decode("latin1") for m in use_rx.finditer(s["content"])})
            k_ops = len(k_rx.findall(s["content"]))
            scope_used = []
            for name in used_names:
                if name not in declared:
                    out["unresolved"].append({"scope": s["scope"], "name": name})
                    continue
                info = declared[name]
                scope_used.append({"scope": s["scope"], "name": name,
                                   "kind": info.get("kind"),
                                   "sha256": info.get("sha256"),
                                   "matchesTarget": (info.get("sha256") == target_sha)
                                   if info.get("sha256") else None})
                if info.get("kind") == "rgb":
                    out["rgbSpaces"].append({"scope": s["scope"], "name": name,
                                             "source": info.get("source")})
                elif info.get("kind") == "cmyk" and info.get("sha256"):
                    out["iccCmykSpaces"].append({"scope": s["scope"], "name": name,
                                                 "sha256": info.get("sha256")})
                    if info.get("sha256") != target_sha:
                        out["wrongIcc"].append({"scope": s["scope"], "name": name,
                                                "sha256": info.get("sha256")})
                elif info.get("kind") == "separation":
                    if info.get("name") != "All":
                        out["spotNames"].append({"scope": s["scope"], "name": name,
                                                 "separation": info.get("name")})
            out["used"].extend(scope_used)
            if s["inherited"] and k_ops:
                out["inheritedForms"] += 1
                out["deviceCMYKInInheritedForms"] += k_ops
            out["scopes"].append({
                "scope": s["scope"], "inherited": s["inherited"],
                "hasOwnResources": s["hasOwnResources"],
                "kOperators": k_ops,
                "declared": {k: v.get("kind") for k, v in declared.items()},
                "used": [u["name"] for u in scope_used],
            })
        out["ok"] = not (out["unresolved"] or out["rgbSpaces"]
                         or out["wrongIcc"] or out["iccCmykSpaces"])
        out["deviceCMYKSource"] = (
            "page /DefaultCMYK" if out["deviceCMYKInInheritedForms"]
            and out["pageDefaultCMYK"] else
            "device-dependent (no /DefaultCMYK)" if out["deviceCMYKInInheritedForms"]
            else "no inherited DeviceCMYK")
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
        out["ok"] = False
    if out["error"]:
        out["status"] = "pending"
    elif out["unresolved"] or out["rgbSpaces"] or out["wrongIcc"] or out["iccCmykSpaces"]:
        out["status"] = "fail"
    elif out["deviceCMYKInInheritedForms"] and not out["pageDefaultCMYK"]:
        out["status"] = "fail"
    elif out["spotNames"]:
        out["status"] = "pending"
    else:
        out["status"] = "pass"
    return out


def stored_ink_check(pdf):
    """Stored CMYK numbers in reachable page/Form content: authoritative file ink."""
    try:
        from pypdf import PdfReader as _R
        import re as _re
        reader = _R(pdf)
        page = reader.pages[0]
        num = rb"(-?(?:\d+(?:\.\d*)?|\.\d+))"
        k_rx = _re.compile(num + rb"\s+" + num + rb"\s+" + num + rb"\s+" + num + rb"\s+(k|K)\b")
        scn_rx = _re.compile(num + rb"\s+" + num + rb"\s+" + num + rb"\s+" + num + rb"\s+(scn|SCN)\b")
        rgb_rx = _re.compile(num + rb"\s+" + num + rb"\s+" + num + rb"\s+(rg|RG)\b")
        counts = {"page": 0, "forms": 0}
        cmyk = {}
        rgb = 0

        def scan(data, scope):
            nonlocal rgb
            for rx in (k_rx, scn_rx):
                for m in rx.finditer(data):
                    vals = tuple(round(float(m.group(i)) * 100.0, 2) for i in range(1, 5))
                    counts[scope] += 1
                    cmyk[vals] = cmyk.get(vals, 0) + 1
            rgb += len(rgb_rx.findall(data))

        page_res = _page_resources(page)
        page_data = _page_content_data(page)
        scan(page_data, "page")
        seen = set()

        def walk(res):
            xo = (res or {}).get("/XObject")
            xo = xo.get_object() if hasattr(xo, "get_object") else (xo or {})
            for _, ref in xo.items():
                obj = ref.get_object()
                if id(obj) in seen:
                    continue
                seen.add(id(obj))
                if str(obj.get("/Subtype")) != "/Form":
                    continue
                try:
                    data = obj.get_data()
                except Exception:  # noqa: BLE001
                    data = b""
                scan(data, "forms")
                fres = obj.get("/Resources")
                walk(fres.get_object() if fres is not None else res)

        walk(page_res)
        total = counts["page"] + counts["forms"]
        dominant = max(cmyk.items(), key=lambda kv: kv[1]) if cmyk else (None, 0)
        tuples = [{"cmyk": list(k), "count": v} for k, v in
                  sorted(cmyk.items(), key=lambda kv: -kv[1])[:12]]
        return {
            "status": "pass" if total else "pending",
            "operators": counts,
            "cmykOperators": total,
            "rgbOperators": rgb,
            "distinctTuples": len(cmyk),
            "tuples": tuples,
            "dominant": list(dominant[0]) if dominant[0] else None,
            "dominantCount": dominant[1],
            "dominantTacPercent": round(sum(dominant[0]), 2) if dominant[0] else None,
            "authoritative": "content-stream operands are the ink the file specifies",
            "note": "Renderer plate values are a device-space reading, not file values. "
                    "Only page-reachable content is scanned.",
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "pending", "error": str(exc)}


def _render_sep_plates(pdf, dpi, tmp, media_box=None, crop_box=None,
                       output_icc=None, source_icc=None):
    out = os.path.join(tmp, "sep.tif")
    cmd = [GS, "-q", "-dBATCH", "-dNOPAUSE", "-dSAFER",
           "-sDEVICE=tiffsep", "-r%d" % dpi, "-sCompression=none",
           "-dTextAlphaBits=1", "-dGraphicsAlphaBits=1"]
    if source_icc:
        cmd.append("-sDefaultCMYKProfile=%s" % source_icc)
    if output_icc:
        cmd.append("-sOutputICCProfile=%s" % output_icc)
    cmd += ["-o", out, pdf]
    subprocess.run(cmd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    names = {}
    for fn in os.listdir(tmp):
        if not fn.startswith("sep(") or not fn.endswith(").tif"):
            continue
        plate = fn[4:-5]
        names[plate] = os.path.join(tmp, fn)
    order = ["Cyan", "Magenta", "Yellow", "Black"]
    if not all(x in names for x in order):
        raise RuntimeError("missing separation plates: %s" % sorted(names))
    plates = [raster.read_tiff(names[x])["data"] for x in order]
    arr = np.stack(plates, axis=-1).astype(np.int16)
    if crop_box and media_box and len(crop_box) == 4 and len(media_box) == 4:
        sc = dpi / 72.0
        x0 = max(0, int(round((crop_box[0] - media_box[0]) * sc)))
        x1 = min(arr.shape[1], int(round((crop_box[2] - media_box[0]) * sc)))
        y0 = max(0, int(round((media_box[3] - crop_box[3]) * sc)))
        y1 = min(arr.shape[0], int(round((media_box[3] - crop_box[1]) * sc)))
        arr = arr[y0:y1, x0:x1]
    return arr


def _dominant_plate_ink(arr):
    h, w, _ = arr.shape
    step = max(1, min(h, w) // 500)
    sample = arr[::step, ::step].reshape(-1, 4)
    from collections import Counter
    counts = Counter(map(tuple, sample.tolist()))
    top, n = counts.most_common(1)[0]
    return {
        "plateBytes": list(top),
        "inkPercent": [round((255 - v) / 2.55, 2) for v in top],
        "coverage": round(n / float(len(sample)), 4),
        "sampledPixels": int(len(sample)),
    }


def _classify_managed(stored_tuples, plate, probe, declared_ok, has_declaration=False):
    """Pure classifier so negative fixtures can exercise it without a renderer.

    stored_tuples: distinct CMYK percent tuples in the content streams.
    plate/probe: dominant CMYK percent tuples read from tiffsep, or None.
    declared_ok: the page /DefaultCMYK is present and is the delivery ICC.
    has_declaration: any /DefaultCMYK exists (even if it is the wrong ICC).
    """
    def nearest(value):
        if value is None or not stored_tuples:
            return None, None
        best = min(stored_tuples,
                   key=lambda t: max(abs(x - y) for x, y in zip(value, t)))
        return best, round(max(abs(x - y) for x, y in zip(value, best)), 3)

    plate_match, d_sp = nearest(plate)
    probe_match, d_pr = nearest(probe) if probe is not None else (None, None)
    flip = d_sp is not None and d_sp > 1.0
    if has_declaration and not declared_ok:
        return {"status": "fail", "mechanism": "declaration_is_not_the_delivery_icc",
                "plateMatchesStored": list(plate_match) if plate_match else None,
                "storedVsPlateMaxDiff": d_sp}
    if not declared_ok:
        if d_sp is not None and d_sp <= 1.0:
            return {"status": "pass", "mechanism": "no_declaration_device_passthrough",
                    "plateMatchesStored": list(plate_match) if plate_match else None,
                    "storedVsPlateMaxDiff": d_sp}
        return {"status": "fail", "mechanism": "unexplained_renderer_divergence",
                "plateMatchesStored": list(plate_match) if plate_match else None,
                "storedVsPlateMaxDiff": d_sp}
    if flip:
        if d_pr is not None and d_pr <= 1.0 and probe != plate:
            return {"status": "pass", "mechanism": "defaultcmyk_reseparation_confirmed_by_removal_probe",
                    "plateMatchesStored": list(plate_match) if plate_match else None,
                    "probeMatchesStored": list(probe_match) if probe_match else None,
                    "storedVsPlateMaxDiff": d_sp, "probeVsStoredMaxDiff": d_pr}
        return {"status": "fail", "mechanism": "removal_probe_did_not_restore_stored_values",
                "plateMatchesStored": list(plate_match) if plate_match else None,
                "probeMatchesStored": list(probe_match) if probe_match else None,
                "storedVsPlateMaxDiff": d_sp, "probeVsStoredMaxDiff": d_pr}
    return {"status": "pass", "mechanism": "device_read_stored_values",
            "plateMatchesStored": list(plate_match) if plate_match else None,
            "storedVsPlateMaxDiff": d_sp}


def _sample_rect(arr, rect):
    h, w = arr.shape[:2]
    x0, y0, x1, y1 = [int(round(v)) for v in rect]
    x0, x1 = max(0, min(w, x0)), max(0, min(w, x1))
    y0, y1 = max(0, min(h, y0)), max(0, min(h, y1))
    if x1 <= x0 or y1 <= y0:
        raise RuntimeError("empty sample rect %s in %sx%s" % (rect, w, h))
    sample = arr[y0:y1, x0:x1].reshape(-1, arr.shape[2])
    values, counts = np.unique(sample, axis=0, return_counts=True)
    top = values[counts.argmax()]
    return [int(x) for x in top], int(counts.max()), int(len(sample))


def _region_pixel_rect(region, bleed, media, dpi):
    r = pdfpaint._region_rect(region, bleed)
    sx0, sy0, sx1, sy1 = [float(v) for v in region["sample"]]
    x0 = r[0] + sx0 * (r[2] - r[0])
    x1 = r[0] + sx1 * (r[2] - r[0])
    y0 = r[1] + sy0 * (r[3] - r[1])
    y1 = r[1] + sy1 * (r[3] - r[1])
    sc = dpi / 72.0
    return [int(round((x0 - media[0]) * sc)),
            int(round((media[3] - y1) * sc)),
            int(round((x1 - media[0]) * sc)),
            int(round((media[3] - y0) * sc))]


def _read_pam(path):
    data = open(path, "rb").read()
    end = data.index(b"ENDHDR\n") + len(b"ENDHDR\n")
    header = data[:end].decode("latin1")
    fields = {}
    for line in header.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] in ("WIDTH", "HEIGHT", "DEPTH", "MAXVAL"):
            fields[parts[0]] = int(parts[1])
    w, h, c = fields["WIDTH"], fields["HEIGHT"], fields["DEPTH"]
    return np.frombuffer(data[end:end + w * h * c], dtype=np.uint8).reshape(h, w, c)


def _control_cmyk_icc():
    """A deliberately different CMYK profile for the renderer control."""
    candidates = []
    for root in ("/opt/homebrew", "/usr/local", "/usr/share"):
        candidates += sorted(glob.glob(
            os.path.join(root, "**", "default_cmyk.icc"), recursive=True))
    candidates.append(
        "/Library/Application Support/Adobe/Color/Profiles/Recommended/USWebCoatedSWOP.icc")
    return next((p for p in candidates if os.path.exists(p)), None)


def managed_separations_check(pdf, stored, contract, job, boxes, target_icc, dpi=150):
    """Region-based managed separation check with an explicit output profile.

    Primary evidence: a tiffsep render with the contract target as both the
    assumed DeviceCMYK source and the explicit output device. The declared
    green sample interiors must read the canonical plate bytes.

    Independent evidence: MuPDF ``-c cmyk`` reads the same stored bytes.

    Negative control: the same renderer with a deliberately different output
    profile must shift the plate values. If it does not, the renderer ignored
    the profile selection and the gate cannot pass.
    """
    result = {"status": "pending", "dpi": dpi, "regions": [],
              "profiles": {"target": target_icc},
              "gsVersion": subprocess.run([GS, "--version"], capture_output=True,
                                          text=True).stdout.strip(),
              "mupdfVersion": (subprocess.run([MUTOOL, "-v"], capture_output=True,
                                              text=True).stderr.strip().splitlines()[0]
                               if MUTOOL else None)}
    if not stored or stored.get("status") != "pass" or not stored.get("tuples"):
        result["error"] = "no stored CMYK operator to compare"
        return result
    bleed = boxes.get("BleedBox")
    media = boxes.get("MediaBox")
    if not bleed or not media or len(bleed) != 4 or len(media) != 4:
        result["error"] = "MediaBox/BleedBox missing"
        result["status"] = "fail"
        return result
    brand_name = colorcontract.brand_color_name(contract)
    node_id = str((job.get("figma") or {}).get("nodeId") or "")
    regions = [r for r in contract["colors"][brand_name]["regions"]
               if str(r.get("jobNode")) == node_id]
    if not regions:
        result["error"] = "no contract region for Figma node %s" % node_id
        result["status"] = "fail"
        return result
    expected_cmyk = [int(x) for x in contract["colors"][brand_name]["cmyk"]["bytes8"]]
    expected_plate = [255 - x for x in expected_cmyk]
    result["expectedPlateBytes"] = expected_plate
    result["expectedCmykBytes"] = expected_cmyk
    control_icc = _control_cmyk_icc()
    result["controlProfile"] = control_icc
    if control_icc:
        result["controlProfileSha256"] = _sha_bytes(open(control_icc, "rb").read())
    if not control_icc:
        result["error"] = "no secondary CMYK profile for the renderer control"
        result["status"] = "fail"
        return result
    if not MUTOOL:
        result["error"] = "mutool not found; install mupdf-tools"
        return result
    try:
        with tempfile.TemporaryDirectory() as tmp:
            prof = _render_sep_plates(pdf, dpi, tmp, media, None,
                                      output_icc=target_icc, source_icc=target_icc)
            ctrl = _render_sep_plates(pdf, dpi, tmp, media, None,
                                      output_icc=control_icc, source_icc=target_icc)
            mu_path = os.path.join(tmp, "mu-cmyk.pam")
            subprocess.run([MUTOOL, "draw", "-q", "-r", str(dpi),
                            "-F", "pam", "-c", "cmyk", "-o", mu_path, pdf],
                           check=True, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            mu = _read_pam(mu_path)
            if mu.shape[2] != 4:
                raise RuntimeError("MuPDF PAM is not 4-channel CMYK: %s" % (mu.shape,))
            ok = True
            for region in regions:
                rect = _region_pixel_rect(region, bleed, media, dpi)
                p_bytes, p_count, p_pixels = _sample_rect(prof, rect)
                c_bytes, c_count, _ = _sample_rect(ctrl, rect)
                m_bytes, m_count, m_pixels = _sample_rect(mu, rect)
                p_ok = all(abs(a - b) <= 1 for a, b in zip(p_bytes, expected_plate))
                m_ok = all(abs(a - b) <= 1 for a, b in zip(m_bytes, expected_cmyk))
                control_delta = max(abs(a - b) for a, b in zip(p_bytes, c_bytes))
                control_ok = control_delta >= 3
                p_coverage = round(p_count / p_pixels, 4)
                m_coverage = round(m_count / m_pixels, 4)
                coverage_ok = p_coverage >= 0.95 and m_coverage >= 0.95
                row = {"label": region["label"], "pixelRect": rect,
                       "plateBytes": p_bytes, "plateCoverage": p_coverage,
                       "plateInkPercent": [round((255 - v) / 2.55, 2) for v in p_bytes],
                       "plateOk": p_ok,
                       "mupdfBytes": m_bytes, "mupdfCoverage": m_coverage,
                       "mupdfOk": m_ok,
                       "coverageOk": coverage_ok,
                       "controlBytes": c_bytes, "controlDelta": control_delta,
                       "controlOk": control_ok}
                if not (p_ok and m_ok and coverage_ok and control_ok):
                    ok = False
                result["regions"].append(row)
            result["argv"] = {
                "profile": [GS, "-sDEVICE=tiffsep", "-r%d" % dpi,
                            "-sDefaultCMYKProfile=" + target_icc,
                            "-sOutputICCProfile=" + target_icc],
                "control": [GS, "-sDEVICE=tiffsep", "-r%d" % dpi,
                            "-sDefaultCMYKProfile=" + target_icc,
                            "-sOutputICCProfile=" + control_icc],
                "mupdf": [MUTOOL, "draw", "-r", str(dpi), "-F", "pam", "-c", "cmyk"],
            }
            result["status"] = "pass" if ok else "fail"
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
        result["status"] = "fail"
    return result


def _read_ppm(path):
    data = open(path, "rb").read()
    pos = [0]

    def tok():
        while pos[0] < len(data):
            if data[pos[0]:pos[0] + 1] == b"#":
                while pos[0] < len(data) and data[pos[0]] not in (10, 13):
                    pos[0] += 1
            elif data[pos[0]:pos[0] + 1].isspace():
                pos[0] += 1
            else:
                break
        start = pos[0]
        while pos[0] < len(data) and not data[pos[0]:pos[0] + 1].isspace():
            pos[0] += 1
        return data[start:pos[0]]

    magic = tok()
    if magic != b"P6":
        raise RuntimeError("not a P6 PPM: %r" % magic)
    w, h, maxv = int(tok()), int(tok()), int(tok())
    pos[0] += 1
    buf = data[pos[0]:pos[0] + w * h * 3]
    if len(buf) < w * h * 3:
        raise RuntimeError("PPM short: %d < %d" % (len(buf), w * h * 3))
    return np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 3)


def _render_rgb(pdf, dpi, tmp, tag, cmyk_icc, rgb_icc):
    """Render one PDF to RGB through explicit ICC destinations.

    Ghostscript: CMYK device -> explicit CMYK output profile -> tificc with
    explicit source/destination. MuPDF: proof through the same CMYK profile.
    No renderer default profile is allowed to decide the colour.
    """
    gs_cmyk = os.path.join(tmp, "%s-gs-cmyk.tif" % tag)
    gs_path = os.path.join(tmp, "%s-gs.tif" % tag)
    mu_path = os.path.join(tmp, "%s-mu.ppm" % tag)
    subprocess.run([GS, "-q", "-dBATCH", "-dNOPAUSE", "-dSAFER",
                    "-sDEVICE=tiff32nc", "-r%d" % dpi,
                    "-sDefaultCMYKProfile=%s" % cmyk_icc,
                    "-sOutputICCProfile=%s" % cmyk_icc,
                    "-o", gs_cmyk, pdf], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["tificc", "-n", "-i" + cmyk_icc, "-o" + rgb_icc,
                    "-t1", "-b", "-w8", gs_cmyk, gs_path], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run([MUTOOL, "draw", "-q", "-r", str(dpi),
                    "-c", "rgb", "-e", cmyk_icc, "-F", "ppm",
                    "-o", mu_path, pdf], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return raster.read_tiff(gs_path)["data"][..., :3], _read_ppm(mu_path)


def _dominant_rgb(arr):
    h, w, _ = arr.shape
    step = max(1, min(h, w) // 400)
    sample = arr[::step, ::step].reshape(-1, 3)
    from collections import Counter
    top, n = Counter(map(tuple, sample.tolist())).most_common(1)[0]
    return {"rgb": [int(x) for x in top], "hex": "#%02X%02X%02X" % top,
            "coverage": round(n / float(len(sample)), 4)}


def _classify_renderers(agreement):
    """Pure classifier for two independent renderers on the same file."""
    if not agreement:
        return {"status": "pending", "error": "no renderer statistics"}
    med = agreement.get("median", 99)
    p99 = agreement.get("p99", 99)
    over5 = agreement.get("over5Percent", 99)
    ok = med <= 2.0 and p99 <= 8.0 and over5 <= 2.0
    return {"status": "pass" if ok else "fail",
            "thresholds": {"median": 2.0, "p99": 8.0, "over5Percent": 2.0}}


def independent_render_check(pdf, ai, media_box, trim_box, cmyk_icc, rgb_icc, dpi=100):
    """Ghostscript vs MuPDF on the same file, cropped to TrimBox."""
    result = {"dpi": dpi, "renderers": {"gs": subprocess.run(
        [GS, "--version"], capture_output=True, text=True).stdout.strip(),
        "mupdf": subprocess.run([MUTOOL, "-v"], capture_output=True,
                                text=True).stderr.strip().splitlines()[0]
        if os.path.exists(MUTOOL) else None}}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            for tag, path in (("final", pdf), ("ai", ai)):
                if not path or not os.path.exists(path):
                    result[tag] = {"status": "pending", "error": "file missing"}
                    continue
                gs, mu = _render_rgb(path, dpi, tmp, tag, cmyk_icc, rgb_icc)
                binfo = pdfprobe.inspect(path).get("boxes", {})
                b_media = binfo.get("MediaBox") or media_box
                b_trim = binfo.get("TrimBox") or b_media
                if b_media and b_trim and len(b_media) == 4 and len(b_trim) == 4:
                    sc = dpi / 72.0
                    x0 = max(0, int(round((b_trim[0] - b_media[0]) * sc)))
                    x1 = min(gs.shape[1], int(round((b_trim[2] - b_media[0]) * sc)))
                    y0 = max(0, int(round((b_media[3] - b_trim[3]) * sc)))
                    y1 = min(gs.shape[0], int(round((b_media[3] - b_trim[1]) * sc)))
                    gs = raster.crop(gs, x0, y0, x1, y1)
                    mu = raster.crop(mu, x0, y0, min(mu.shape[1], x1), min(mu.shape[0], y1))
                hh = min(gs.shape[0], mu.shape[0])
                ww = min(gs.shape[1], mu.shape[1])
                gs = raster.resize_nearest(gs, ww, hh)
                mu = raster.resize_nearest(mu, ww, hh)
                cmp = raster.compare_aligned(gs, mu, max_shift=2, flat=True, flat_radius=3)
                agreement = {k: cmp.get(k) for k in
                             ("median", "p90", "p99", "over5Percent", "flatOnly", "pixels")}
                verdict = _classify_renderers(agreement)
                result[tag] = {"status": verdict["status"],
                               "agreement": agreement,
                               "gsDominant": _dominant_rgb(gs),
                               "mupdfDominant": _dominant_rgb(mu),
                               "thresholds": verdict.get("thresholds")}
            final_ok = (result.get("final") or {}).get("status") == "pass"
            ai_status = (result.get("ai") or {}).get("status")
            if not final_ok:
                result["status"] = "fail" if (result.get("final") or {}).get("status") == "fail" else "pending"
            elif ai_status in ("pending", "fail"):
                # The .ai is a reference artifact: it has no document CMYK ICC
                # or OutputIntent. Its renderer disagreement is recorded, not
                # used to block the authoritative PDF.
                result["status"] = "pass"
                result["aiReference"] = {
                    "status": ai_status,
                    "note": "AI is reference only; not a print input. PDF renderers agree.",
                }
            else:
                result["status"] = "pass"
    except Exception as exc:  # noqa: BLE001
        result.update({"status": "pending", "error": str(exc)})
    return result


def profilechain_check(conv, contract_sha, contract_target_sha, oi_sha):
    """One contract + one executed conversion + one OutputIntent must agree."""
    if not conv:
        return {"status": "pending", "error": "work/conversion.json missing",
                "contractSha256": contract_sha,
                "contractTargetSha256": contract_target_sha,
                "outputIntentSha256": oi_sha}
    recorded = conv.get("targetICC")
    recorded_sha = conv.get("targetICCsha256")
    vals = {"contract": contract_target_sha, "conversion": recorded_sha,
            "outputIntent": oi_sha}
    present = [v for v in vals.values() if v]
    same = bool(len(present) == 3 and len(set(present)) == 1)
    contract_matches = bool(contract_sha and conv.get("colorContractSha256") == contract_sha)
    ok = same and contract_matches
    return {"status": "pass" if ok else "fail",
            "recordedIn": "work/conversion.json",
            "recordedPath": recorded,
            "sha256": vals,
            "colorContractSha256": contract_sha,
            "conversionContractSha256": conv.get("colorContractSha256"),
            "contractMatchesConversion": contract_matches,
            "note": "Contract, executed conversion and OutputIntent must carry one ICC; "
                    "the conversion record must name the current colour contract."
            if ok else "Contract / conversion / OutputIntent mismatch or stale contract."}


def fold_check(job, spec, boxes):
    """A3/A4 fold: fold line at the trim centre and equal panels.

    Only runs when job.fold is declared. A job without fold metadata is not a
    fold job; this returns pass with declared=false rather than inventing one.
    """
    fold = job.get("fold")
    trim = boxes.get("TrimBox")
    if not fold:
        return {"status": "pass", "declared": False,
                "note": "no fold metadata; fold check not applicable"}
    if not trim or len(trim) != 4:
        return {"status": "pending", "declared": True, "error": "TrimBox missing"}
    tw = pt2mm(trim[2] - trim[0])
    th = pt2mm(trim[3] - trim[1])
    axis = str(fold.get("axis") or "vertical")
    ftype = str(fold.get("type") or "half")
    span = tw if axis == "vertical" else th
    position = fold.get("positionMm")
    panels = fold.get("panels")
    errors = []
    if position is not None:
        try:
            position = float(position)
        except (TypeError, ValueError):
            errors.append("fold.positionMm is not numeric")
            position = None
    if position is None and ftype == "half":
        position = round(span / 2.0, 3)
    if position is None:
        errors.append("fold.positionMm missing")
    elif position <= 0 or position >= span:
        errors.append("fold line outside trim: %.2f mm of %.2f mm" % (position, span))
    elif ftype == "half" and abs(position - span / 2.0) > 0.5:
        errors.append("half fold is not at trim centre: %.2f vs %.2f mm"
                      % (position, span / 2.0))
    if panels:
        try:
            panels = [float(x) for x in panels]
        except (TypeError, ValueError):
            panels = None
            errors.append("fold.panels is not numeric")
        if panels:
            if abs(sum(panels) - span) > 0.5:
                errors.append("panels sum %.2f mm != trim span %.2f mm"
                              % (sum(panels), span))
            if ftype == "half" and max(panels) - min(panels) > 0.5:
                errors.append("half-fold panels differ: %s" % panels)
    mark_pt = None
    if position is not None:
        mark_pt = (round(trim[0] + mm2pt(position), 3) if axis == "vertical"
                   else round(trim[1] + mm2pt(position), 3))
    return {
        "status": "pass" if not errors else "fail",
        "declared": True,
        "type": ftype,
        "axis": axis,
        "trimSpanMm": round(span, 2),
        "positionMm": position,
        "panelsMm": panels,
        "foldMarkCoordinatePt": mark_pt,
        "errors": errors,
        "note": "Marks geometry is validated separately by pdf.marks.geometry; "
                "this check ties the fold line to the trim and panel widths.",
    }


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


def bleed_content(pdf, boxes, bleed_mm, target_icc, dpi=300):
    media = boxes.get("MediaBox")
    trim = boxes.get("TrimBox")
    if not media or not trim or len(media) != 4 or len(trim) != 4:
        return {"status": "pending", "error": "MediaBox/TrimBox missing"}
    if pt2mm(media[2] - media[0]) <= pt2mm(trim[2] - trim[0]) + 0.5:
        return {"status": "pending", "error": "final PDF has no bleed area (MediaBox=TrimBox)"}
    with tempfile.TemporaryDirectory() as tmp:
        tif = os.path.join(tmp, "p.tif")
        subprocess.run([GS, "-q", "-dBATCH", "-dNOPAUSE", "-dSAFER", "-sDEVICE=tiff32nc",
                        "-sDefaultCMYKProfile=%s" % target_icc, "-dOverrideICC=true",
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


def visual_compare(final_pdf, ai, target_icc, rgb_icc, dpi=150, crop_mm=0.0, media_box=None, crop_box=None):
    with tempfile.TemporaryDirectory() as tmp:
        f_cmyk = os.path.join(tmp, "final-cmyk.tif")
        f_sim = os.path.join(tmp, "final-sim.tif")
        a_cmyk = os.path.join(tmp, "ai-cmyk.tif")
        a_sim = os.path.join(tmp, "ai-sim.tif")
        subprocess.run([GS, "-q", "-dBATCH", "-dNOPAUSE", "-dSAFER", "-sDEVICE=tiff32nc",
                        "-sDefaultCMYKProfile=%s" % target_icc,
                        "-sOutputICCProfile=%s" % target_icc,
                        "-r%d" % dpi, "-o", f_cmyk, final_pdf], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["tificc", "-n", "-i" + target_icc, "-o" + rgb_icc,
                        "-t1", "-b", "-w8", f_cmyk, f_sim], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run([GS, "-q", "-dBATCH", "-dNOPAUSE", "-dSAFER", "-sDEVICE=tiff32nc",
                        "-sDefaultCMYKProfile=%s" % target_icc,
                        "-sOutputICCProfile=%s" % target_icc,
                        "-r%d" % dpi, "-o", a_cmyk, ai], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["tificc", "-n", "-i" + target_icc, "-o" + rgb_icc,
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
    job_dir = os.path.dirname(pdf)
    if os.path.basename(pdf) == "cmyk.pdf":
        job_dir = os.path.dirname(job_dir)
    checks = []
    try:
        color_contract = colorcontract.load()
        spec = colorcontract.apply_to_spec(spec, color_contract)
        contract_sha = colorcontract.contract_sha(color_contract)
        brand_name = colorcontract.brand_color_name(color_contract)
        target_icc, target_profile = colorcontract.profile(color_contract, "target")
        target_sha = target_profile["sha256"]
        source_icc, source_profile = colorcontract.profile(color_contract, "source")
    except Exception as exc:  # noqa: BLE001
        checks.append(check("color.contract", "Colour contract", "fail", str(exc),
                            "loads; ICC files exist; SHA-256 matches",
                            "No colour check may run without the contract."))
        return {
            "ok": True, "selftest": bool(selftest),
            "jobId": job.get("jobId"), "pdf": pdf,
            "sha256": info.get("sha256"), "aiSha256": None,
            "acceptanceHash": None, "overall": "fail",
            "counts": {"pass": 0, "fail": 1, "pending": 0},
            "checks": checks,
            "limits": ["Gate 3 stopped: the colour contract is not valid."],
        }
    conv = load(os.path.join(job_dir, "work", "conversion.json")) or {}
    recorded_icc = conv.get("targetICC") if conv else None
    if recorded_icc and os.path.exists(recorded_icc):
        target_icc_source = "work/conversion.json"
    else:
        target_icc_source = "config/color-contract.json (conversion record missing)"
    tw, th = trim_mm(job, spec)
    b = float(job.get("bleedMm", spec["print"].get("bleedMm", 0)) or 0)
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
        bc = bleed_content(pdf, boxes, b, target_icc)
        checks.append(check("pdf.bleedcontent", "Final PDF bleed content",
                            bc.get("status", "pending"), bc,
                            "No pure white within 3 mm on all four sides; PENDING when there is no bleed area",
                            "Page boxes alone are not enough; check the actual ink"))
    fold = fold_check(job, spec, boxes)
    checks.append(check("pdf.fold", "A3/A4 fold line and panels", fold.get("status"),
                        fold, "Fold line at trim centre; panels equal and summing to the trim span; "
                              "a job without fold metadata is not applicable",
                        "A3 half fold to A4: 420 = 210 + 210; mark geometry is checked by pdf.marks"))

    # Output intent / embedded CMYK profile / PDF/X identification
    contract = (job.get("pdfExport") or {}).get("contract")
    oi = st.get("outputIntent", 0)
    chain = pdfprobe.output_intents(pdf)
    cond = chain[0]["condition"] if chain else None
    dest_profile = chain[0]["destProfile"] if chain else None
    pdfx_required = spec["print"].get("pdfxStandard")
    profiles = info.get("profiles", [])
    dcm_absence = defaultcmyk_absence_check(pdf)
    header_intent = None
    try:
        icc_bytes = open(target_icc, "rb").read()
        if len(icc_bytes) >= 68:
            header_intent = int.from_bytes(icc_bytes[64:68], "big")
    except Exception:  # noqa: BLE001
        pass
    headers = raw[:8].decode("latin1")
    hdr_ok = headers.startswith("%PDF-1.6")
    intent_ok = (len(chain) == 1 and bool(dest_profile)
                 and chain[0].get("subtype") == "/GTS_PDFX"
                 and dest_profile.get("sha256") == target_sha
                 and dest_profile.get("isCMYK") and not dest_profile.get("isRGB")
                 and cond == target_profile["outputConditionIdentifier"]
                 and (chain[0].get("registry") or "") == target_profile["registryName"]
                 and not re.search(rb"/DestOutputProfileRef", raw))
    checks.append(check("pdf.outputintent", "GTS_PDFX OutputIntent + target ICC",
                        "pass" if (intent_ok and hdr_ok) else "fail",
                        {"count": len(chain), "condition": cond,
                         "subtype": chain[0].get("subtype") if chain else None,
                         "expectedSubtype": "/GTS_PDFX",
                         "expectedCondition": target_profile["outputConditionIdentifier"],
                         "registry": chain[0].get("registry") if chain else None,
                         "expectedRegistry": target_profile["registryName"],
                         "header": headers.strip(),
                         "destProfile": dest_profile,
                         "iccSha256": target_sha,
                         "iccHeaderIntent": header_intent,
                         "conversionIntent": conv.get("renderingIntent"),
                         "intentObservation": (
                             "ICC header intent %s vs conversion intent %s; no /Intent key. "
                             "Printer RIP behaviour is unverified (RCA F14)."
                             % (header_intent, conv.get("renderingIntent"))
                             if header_intent is not None and conv.get("renderingIntent") is not None
                             and header_intent != conv.get("renderingIntent")
                             else "intent is pinned by the recorded conversion argv"),
                         "profiles": [{k: x.get(k) for k in ("description", "space", "sha256")}
                                      for x in profiles]},
                        "Exactly 1 GTS_PDFX; DestOutputProfile = the contract delivery ICC; PDF 1.6",
                        "Both the ICC hash and the structure must match; a label cannot fake a conversion"))
    checks.append(check("pdf.nodefaultcmyk", "No remappable ICCBased /DefaultCMYK",
                        dcm_absence.get("status", "pending"), dcm_absence,
                        "No ICCBased /DefaultCMYK on the page or any reachable Form; "
                        "bare DeviceCMYK is interpreted by the single CMYK OutputIntent",
                        "PDF/X-4 does not require /DefaultCMYK; an identical ICCBased CMYK "
                        "default space re-separates at the reader (RCA 2026-11-16) and is excluded by policy"))
    chain_result = profilechain_check(conv, contract_sha, target_sha,
                                      (dest_profile or {}).get("sha256"))
    checks.append(check("pdf.profilechain", "ICC binding chain (contract / conversion / output intent)",
                        chain_result.get("status"), chain_result,
                        "One ICC hash across config/color-contract.json, "
                        "work/conversion.json and DestOutputProfile",
                        "Audit and render read the recorded conversion profile; no library constant"))

    # PDF/X-4 structural conformance: internal Preflight equivalent over
    # file facts. It does not replace Acrobat Preflight or the printer's own
    # preflight, and it never upgrades a plain PDF by relabelling.
    subs = {}
    try:
        from pypdf import PdfReader as _XReader
        from pypdf.generic import NameObject as _NameObject
        _xr = _XReader(pdf)
        _raw_info = _xr.metadata or {}
        _mi = {str(k): str(v) for k, v in _raw_info.items()}
        subs["infoGtsPdfxVersion"] = _mi.get("/GTS_PDFXVersion")
        subs["infoTrapped"] = (str(_mi.get("/Trapped")).lstrip("/")
                               if _mi.get("/Trapped") is not None else None)
        subs["infoTrappedName"] = isinstance(_raw_info.get("/Trapped"), _NameObject)
        subs["infoCreationDate"] = bool(_mi.get("/CreationDate"))
        _xmp_ref = _xr.trailer["/Root"].get("/Metadata")
        if _xmp_ref is not None:
            _xmp_ref = _xmp_ref.get_object()
        _xmp_txt = (_xmp_ref.get_data().decode("utf-8", "replace")
                    if _xmp_ref is not None else "")
        subs["xmpPresent"] = bool(_xmp_txt)
        try:
            ET.fromstring(_xmp_txt)
            subs["xmpParses"] = True
        except Exception:
            subs["xmpParses"] = False
        _mg = re.search(r"pdfxid:GTS_PDFXVersion>([^<]+)<", _xmp_txt)
        subs["xmpGtsPdfxVersion"] = _mg.group(1) if _mg else None
        subs["xmpTitle"] = bool(re.search(r"dc:title", _xmp_txt))
        subs["xmpMetadataDate"] = bool(re.search(r"xmp:MetadataDate>([^<]+)<", _xmp_txt))
        subs["xmpDocumentId"] = bool(re.search(r"xmpMM:DocumentID>([^<]+)<", _xmp_txt))
        subs["xmpInstanceId"] = bool(re.search(r"xmpMM:InstanceID>([^<]+)<", _xmp_txt))
        subs["xmpVersionId"] = bool(re.search(r"xmpMM:VersionID>([^<]+)<", _xmp_txt))
        subs["xmpRenditionClass"] = bool(re.search(r"xmpMM:RenditionClass>([^<]+)<", _xmp_txt))
        _mt = re.search(r"pdf:Trapped>([^<]+)<", _xmp_txt)
        subs["xmpTrapped"] = _mt.group(1) if _mt else None
        subs["trappedMatch"] = (subs.get("infoTrapped") == subs.get("xmpTrapped"))
        subs["pdfxidPropertyCount"] = len(re.findall(r"<pdfxid:[A-Za-z]", _xmp_txt))
        subs["trailerId"] = bool(_xr.trailer.get("/ID"))
        subs["artBoxAbsent"] = _xr.pages[0].get("/ArtBox") is None
        subs["defaultCMYKAbsent"] = dcm_absence.get("status") == "pass"
        subs["outputConditionIdentifier"] = cond
        subs["outputIntentSubtype"] = chain[0].get("subtype") if chain else None
        subs["noExternalProfileRef"] = not re.search(rb"/DestOutputProfileRef", raw)
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
                   and subs.get("infoTrappedName")
                   and subs.get("infoCreationDate")
                   and subs.get("xmpPresent")
                   and subs.get("xmpParses")
                   and subs.get("xmpGtsPdfxVersion") == pdfx_required
                   and subs.get("xmpTitle")
                   and subs.get("xmpMetadataDate")
                   and subs.get("xmpDocumentId")
                   and subs.get("xmpInstanceId")
                   and subs.get("xmpVersionId")
                   and subs.get("xmpRenditionClass")
                   and subs.get("xmpTrapped") == "False"
                   and subs.get("trappedMatch")
                   and subs.get("pdfxidPropertyCount") == 1
                   and subs.get("trailerId")
                   and subs.get("artBoxAbsent")
                   and subs.get("defaultCMYKAbsent")
                   and subs.get("noExternalProfileRef")
                   and subs.get("outputConditionIdentifier")
                   == target_profile["outputConditionIdentifier"]
                   and subs.get("outputIntentSubtype") == "/GTS_PDFX"
                   and subs.get("noPS") and subs.get("noActions")
                   and subs.get("noEncryption") and subs.get("noDeviceRGB"))
        pdfx_status = "pass" if pdfx_ok else "fail"
    checks.append(check("pdf.pdfx", "PDF/X-4 structure self-check", pdfx_status,
                        {"required": pdfx_required,
                         "revision": color_contract["pdf"]["standardRevision"],
                         "subs": subs},
                        "Info + XMP version PDF/X-4, Trapped is a name, XMP dates/IDs, "
                        "trailer /ID, no ArtBox, no /DefaultCMYK, no /PS, no Action/JS, "
                        "no encryption, no RGB",
                        "Internal structure self-check; it does not replace Acrobat Preflight or the print shop check"))

    # Colour operators in page content
    checks.append(check("pdf.rgb", "RGB colour operators in page content",
                        "pass" if rgb_ops == 0 else "fail",
                        rgb_ops, 0,
                        "CMYK operators=%d; /DeviceRGB byte count=%d (may come from a thumbnail)" % (
                            cmyk_ops, st.get("deviceRGB", 0))))
    checks.append(check("pdf.cmyk", "CMYK colour operators in page content",
                        "pass" if cmyk_ops > 0 else "fail", cmyk_ops, "> 0", ""))
    stored = stored_ink_check(pdf)
    checks.append(check("pdf.inkStored", "Stored CMYK values (content stream)",
                        stored.get("status", "pending"), stored,
                        "Read k/K and four-component scn/SCN operands directly; these are the file ink values",
                        "A separation plate reading is a device-space equivalent and cannot recover the stored operand (RCA 2026-11-16)"))
    brand = pdfpaint.brand_green_check(pdf, color_contract, job, boxes, target_icc)
    declared_green = {}
    for _name, _entry in (color_contract.get("colors") or {}).items():
        _cmyk = _entry.get("cmyk") or {}
        if _cmyk.get("percent"):
            declared_green[_name] = (_cmyk["percent"],
                                     float(_cmyk.get("maxPercentDeviation", 0.05)))
    all_green = []
    seen_green = set()
    green_cache = {}
    for _c in colours:
        if _c.get("space") != "cmyk" or not _c.get("cmyk"):
            continue
        _key = tuple(round(float(x), 2) for x in _c["cmyk"])
        if _key in seen_green:
            continue
        seen_green.add(_key)
        _info = pdfpaint._greenish(target_icc, _key, green_cache)
        if _info["isGreen"]:
            _matches = [_n for _n, (_p, _t) in declared_green.items()
                        if pdfpaint._close(_key, _p, _t)]
            all_green.append({"cmyk": list(_key), "count": _c.get("count"),
                              "hue": _info["hue"], "chroma": _info["chroma"],
                              "matches": _matches,
                              "isCanonical": brand_name in _matches})
    brand["allGreenTuples"] = all_green
    if any(not x["matches"] for x in all_green):
        brand["status"] = "fail"
    if not any(x["isCanonical"] for x in all_green):
        brand["status"] = "fail"
    _brand_cmyk = color_contract["colors"][brand_name]["cmyk"]
    checks.append(check("pdf.brandgreen", "single canonical %s" % brand_name,
                        brand.get("status", "pending"), brand,
                        "Every declared region is covered by exactly one opaque, Normal, "
                        "non-overprinting DeviceCMYK event; stored value %s (8-bit %s); "
                        "every greenish operator matches a declared contract colour (%s)"
                        % ("/".join("%.1f" % x for x in _brand_cmyk["percent"]),
                           "/".join(str(x) for x in _brand_cmyk["bytes8"]),
                           ", ".join(sorted(declared_green))),
                        "Regions come from the content stream and the Figma bbox; plate values "
                        "are verified separately by pdf.separationsManaged with an explicit output ICC"))
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
        _stream = _page_content_data(_PdfReader(pdf).pages[0])
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
        _contents = _page_content_data(_pg)
        _content = _contents
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
        _mdata = _page_content_data(_mp)
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
    inherited = inheritedcs_check(pdf, target_sha)
    checks.append(check("pdf.inheritedcs", "Inherited colour spaces (page / Form XObject)",
                        inherited.get("status", "pending"),
                        {k: inherited.get(k) for k in
                         ("used", "scopes", "unresolved", "rgbSpaces", "wrongIcc",
                          "iccCmykSpaces", "spotNames", "inheritedForms",
                          "deviceCMYKInInheritedForms", "pageDefaultCMYK",
                          "deviceCMYKSource", "error")},
                        "Every cs/CS name resolves; artwork uses only DeviceCMYK + the OutputIntent; "
                        "no ICCBased CMYK or RGB inherited space",
                        "PDF/X-4 does not require /DefaultCMYK; this workflow excludes it, so "
                        "DeviceCMYK has no inherited ambiguity"))
    media = boxes.get("MediaBox")
    managed = managed_separations_check(pdf, stored, color_contract, job, boxes,
                                        target_icc)
    checks.append(check("pdf.separationsManaged", "Managed separations (region samples + explicit output ICC)",
                        managed.get("status", "pending"),
                        {k: managed.get(k) for k in
                         ("regions", "expectedPlateBytes", "expectedCmykBytes",
                          "profiles", "controlProfile", "argv", "dpi",
                          "gsVersion", "mupdfVersion", "error")},
                        "For each brand-colour region sample: tiffsep with the explicit output ICC "
                        "must read the inverted stored bytes; MuPDF -c cmyk must read the stored bytes; "
                        "a different output ICC must shift the plate values (proves a managed render)",
                        "The old policy accepted a device-default SWOP re-separation as PASS; "
                        "the /DefaultCMYK deletion probe is retained as historical evidence only"))
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
                visual = visual_compare(pdf, ai, target_icc, source_icc, dpi=150,
                                        media_box=media, crop_box=trim)
            else:
                crop_mm = 0.0
                if media and trim and len(media) == 4 and pt2mm(media[2] - media[0]) > tw + 0.5:
                    crop_mm = b
                visual = visual_compare(pdf, ai, target_icc, source_icc,
                                        dpi=150, crop_mm=crop_mm)
            visual["renderProfile"] = {"path": target_icc,
                                       "source": target_icc_source,
                                       "sha256": hashlib.sha256(
                                           open(target_icc, "rb").read()).hexdigest()[:16]}
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

    # Independent renderers: Ghostscript and MuPDF must agree on the same file.
    # They are not expected to agree between PDF and AI when the AI lacks an
    # ICC declaration; that reference-only difference is recorded, not gated.
    independent = independent_render_check(pdf, ai if os.path.exists(ai) else None,
                                           boxes.get("MediaBox"), trim,
                                           target_icc, source_icc)
    if independent.get("final", {}).get("status") == "pass":
        independent["referenceAppearance"] = {
            "finalGs": (independent.get("final") or {}).get("gsDominant"),
            "finalMuPdf": (independent.get("final") or {}).get("mupdfDominant"),
            "aiGs": (independent.get("ai") or {}).get("gsDominant"),
            "aiMuPdf": (independent.get("ai") or {}).get("mupdfDominant"),
            "note": "AI appearance may differ; it has no document CMYK ICC/OutputIntent. "
                    "PDF is the printer input.",
        }
    checks.append(check("pdf.independentRender", "Independent renderers (Ghostscript / MuPDF)",
                        independent.get("status", "pending"), independent,
                        "Both renderers agree on the same file inside the TrimBox: median ≤ 2, p99 ≤ 8, over5 ≤ 2%",
                        "RCA 2026-11-16: agreement across two engines shows the reading is not a single-tool artefact"))

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
    if selftest:
        conv_path = os.path.join(job_dir, "work", "conversion.json")
        if not os.path.exists(conv_path):
            try:
                _contract = colorcontract.load()
                _target, _profile = colorcontract.profile(_contract, "target")
                with open(conv_path, "w", encoding="utf-8") as fh:
                    json.dump({"targetICC": _target,
                               "targetICCsha256": _profile["sha256"],
                               "colorContractSha256": colorcontract.contract_sha(_contract)},
                              fh, ensure_ascii=False, indent=2)
            except Exception:  # noqa: BLE001
                pass
    result = analyse(pdf, job, spec, selftest=selftest)
    if selftest:
        expected = {
            "pdf.outputintent": "fail",
            "pdf.nodefaultcmyk": "pass",
            "pdf.profilechain": "fail",
            "pdf.fonts": "pass",
            "pdf.pdfx": "fail",
            "pdf.rgb": "pass",
            "pdf.cmyk": "pass",
            "pdf.inkStored": "pass",
            "pdf.inheritedcs": "pass",
            "pdf.brandgreen": "fail",
            "pdf.separationsManaged": "fail",
            "pdf.fold": "pass",
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
