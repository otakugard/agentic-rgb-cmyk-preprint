#!/usr/bin/env python3
"""07c-finalize-pdf.py <job-id>

Turn the approved CMYK intermediate (work/cmyk-final.pdf) into the delivery
candidate:

  - page MediaBox/CropBox = trim + 2 x margin (default 14 mm)
  - TrimBox = trim, BleedBox = trim + 3 mm, ArtBox = TrimBox
  - place the 303x216 bleed artwork at BleedBox
  - draw the jp-double mark set (Japanese double tombo + centre crosses),
    offset 3 mm, 0.3 pt registration lines, no colour bars
  - add the delivery ICC (spec.print.iccProfile) as /DefaultCMYK in page
    resources AND as the DestOutputProfile of a GTS_PDFX OutputIntent
  - write PDF/X-4:2010 identification: Info /GTS_PDFXVersion + /Trapped,
    XMP Metadata (pdfxid:GTS_PDFXVersion, dc:title, pdf:Producer)
  - bind the artwork, final PDF and ICC hashes in work/pdf-finalize.json

The raw Illustrator export is no longer merged: its marks are US-style single
lines and the cross-writer resource copy was unsafe. Marks are generated here
so every job gets the same Japanese-style geometry for every job.
"""

import hashlib
import json
import os
import re
import sys
from datetime import datetime

try:
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import (ArrayObject, DecodedStreamObject,
                               DictionaryObject, FloatObject, NameObject,
                               NumberObject, TextStringObject)
except ImportError:
    print("pypdf is required: python3 -m pip install --user --break-system-packages pypdf", file=sys.stderr)
    sys.exit(78)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "lib"))
import marks as markslib  # noqa: E402


def load(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def mm2pt(v):
    return v * 72 / 25.4


def box_mm(box):
    return [round((box[2] - box[0]) * 25.4 / 72, 3),
            round((box[3] - box[1]) * 25.4 / 72, 3)]


def main():
    if len(sys.argv) != 2:
        print("usage: 07c-finalize-pdf.py <job-id>", file=sys.stderr)
        return 64
    job_id = sys.argv[1]
    job_dir = os.path.join(ROOT, "jobs", job_id)
    job = load(os.path.join(job_dir, "job.json"))
    spec = load(os.path.join(ROOT, "config", "print-spec.json"))
    if not job or not spec:
        print("missing job.json or print-spec.json", file=sys.stderr)
        return 66

    exp = job.get("pdfExport") or {}
    file_name = exp.get("fileName") or ""
    if not re.match(r"^[A-Za-z0-9._-]+\.pdf$", file_name):
        print("job.pdfExport.fileName must be a single .pdf filename", file=sys.stderr)
        return 65
    out = os.path.join(job_dir, file_name)
    base_path = os.path.join(job_dir, "work", "cmyk-final.pdf")
    if not os.path.exists(base_path):
        print("missing work/cmyk-final.pdf (bleed artwork)", file=sys.stderr)
        return 66
    if os.path.realpath(base_path) == os.path.realpath(out):
        print("base PDF and final PDF are the same path", file=sys.stderr)
        return 65

    trim = job.get("trim") or {}
    if trim.get("w") and trim.get("h"):
        trim_w, trim_h = float(trim["w"]), float(trim["h"])
    elif trim.get("preset"):
        trim_w, trim_h = [float(x) for x in spec["trimPresetsMm"][trim["preset"]]]
    else:
        print("job.trim cannot be resolved", file=sys.stderr)
        return 66
    bleed = float(job.get("bleedMm", spec["print"].get("bleedMm", 0)) or 0)
    margin = float(exp.get("marginMm", 14))
    if margin <= bleed:
        print("marginMm must be greater than bleedMm", file=sys.stderr)
        return 65

    page_w = mm2pt(trim_w + 2 * margin)
    page_h = mm2pt(trim_h + 2 * margin)
    trim_x0 = mm2pt(margin)
    trim_y0 = mm2pt(margin)
    trim_x1 = trim_x0 + mm2pt(trim_w)
    trim_y1 = trim_y0 + mm2pt(trim_h)
    bleed_x0 = mm2pt(margin - bleed)
    bleed_y0 = mm2pt(margin - bleed)
    bleed_x1 = trim_x1 + mm2pt(bleed)
    bleed_y1 = trim_y1 + mm2pt(bleed)

    reader = PdfReader(base_path)
    if len(reader.pages) != 1:
        print("base PDF must have exactly 1 page", file=sys.stderr)
        return 65
    writer = PdfWriter()
    writer.append(reader)
    writer.pdf_header = b"%PDF-1.6"
    page = writer.pages[0]

    marks_cfg_path = os.path.join(ROOT, "config", "marks.json")
    mcfg = markslib.config(load(marks_cfg_path) or {})
    source = str(exp.get("marksSource") or mcfg.get("source") or "drawn")
    if source != "drawn":
        print("marks.source=%r is retired; use git revert to restore legacy raw marks" % source,
              file=sys.stderr)
        return 65
    mark_bytes = markslib.content([trim_x0, trim_y0, trim_x1, trim_y1], mcfg)

    base_data = page.get_contents().get_data()
    stream = DecodedStreamObject()
    stream.set_data(("q 1 0 0 1 %.6f %.6f cm\n" % (bleed_x0, bleed_y0)).encode("ascii")
                    + base_data + b"\nQ\n" + mark_bytes)
    page[NameObject("/Contents")] = writer._add_object(stream)

    # Registration colour for the marks: 100% of every plate.
    resources = page["/Resources"].get_object()
    cs = resources.get("/ColorSpace")
    if cs is None:
        cs = DictionaryObject()
        resources[NameObject("/ColorSpace")] = cs
    else:
        cs = cs.get_object()
    tint = DictionaryObject()
    tint[NameObject("/FunctionType")] = NumberObject(2)
    tint[NameObject("/Domain")] = ArrayObject([NumberObject(0), NumberObject(1)])
    tint[NameObject("/C0")] = ArrayObject([NumberObject(0)] * 4)
    tint[NameObject("/C1")] = ArrayObject([NumberObject(1)] * 4)
    tint[NameObject("/N")] = NumberObject(1)
    cs[NameObject("/CSAll")] = ArrayObject([
        NameObject("/Separation"), NameObject("/All"), NameObject("/DeviceCMYK"), tint])

    box = ArrayObject([FloatObject(0), FloatObject(0),
                       FloatObject(page_w), FloatObject(page_h)])
    trim_box = ArrayObject([FloatObject(trim_x0), FloatObject(trim_y0),
                            FloatObject(trim_x1), FloatObject(trim_y1)])
    bleed_box = ArrayObject([FloatObject(bleed_x0), FloatObject(bleed_y0),
                             FloatObject(bleed_x1), FloatObject(bleed_y1)])
    page[NameObject("/MediaBox")] = box
    page[NameObject("/CropBox")] = box
    page[NameObject("/TrimBox")] = trim_box
    page[NameObject("/BleedBox")] = bleed_box
    page[NameObject("/ArtBox")] = trim_box

    # PDF/X-4:2010 (spec.print.pdfxStandard): one embedded ICC shared by
    # /DefaultCMYK and the GTS_PDFX OutputIntent's DestOutputProfile.
    icc_path = spec["print"]["iccProfile"]
    icc_bytes = open(icc_path, "rb").read()
    icc_sha = hashlib.sha256(icc_bytes).hexdigest()
    icc = DecodedStreamObject()
    icc.set_data(icc_bytes)
    icc[NameObject("/N")] = NumberObject(4)
    icc[NameObject("/Alternate")] = NameObject("/DeviceCMYK")
    icc_ref = writer._add_object(icc)
    # /DefaultCMYK must be a colour space: [/ICCBased <stream>].
    cs[NameObject("/DefaultCMYK")] = ArrayObject([NameObject("/ICCBased"), icc_ref])

    _contracts = {"pdfx-4-2010": "PDF/X-4:2010", "pdfx-4": "PDF/X-4",
                  "pdfx-3-2003": "PDF/X-3:2003", "pdfx-1a-2003": "PDF/X-1a:2003"}
    _contract = str(exp.get("contract") or "").strip().lower()
    pdfx_standard = (_contracts.get(_contract)
                     or job.get("pdfxStandard")
                     or spec["print"].get("pdfxStandard", "PDF/X-4:2010"))
    ocid = spec["print"].get("outputConditionID", "ISO Coated v2 300% (ECI)")
    oc = spec["print"].get("outputCondition", ocid)
    intent = DictionaryObject()
    intent[NameObject("/Type")] = NameObject("/OutputIntent")
    intent[NameObject("/S")] = NameObject("/GTS_PDFX")
    intent[NameObject("/OutputConditionIdentifier")] = TextStringObject(ocid)
    intent[NameObject("/OutputCondition")] = TextStringObject(oc)
    intent[NameObject("/RegistryName")] = TextStringObject("http://www.color.org")
    intent[NameObject("/DestOutputProfile")] = icc_ref
    intent[NameObject("/Info")] = TextStringObject(ocid)
    writer._root_object[NameObject("/OutputIntents")] = ArrayObject([intent])

    now = datetime.now().astimezone()
    off = now.strftime("%z")
    info_date = ("D:" + now.strftime("%Y%m%d%H%M%S")
                 + off[:3] + "'" + off[3:5] + "'")
    xmp_date = (now.strftime("%Y-%m-%dT%H:%M:%S")
                + off[:3] + ":" + off[3:5])
    title = file_name[:-4]
    producer = "07c-finalize-pdf.py (pypdf)"
    # Do not name an Illustrator version here: this script never runs
    # Illustrator, and the artwork may come from any supported release.
    creator = "Adobe Illustrator"
    xmp = ('<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
           '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
           ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
           '  <rdf:Description rdf:about=""\n'
           '    xmlns:dc="http://purl.org/dc/elements/1.1/"\n'
           '    xmlns:pdf="http://ns.adobe.com/pdf/1.3/"\n'
           '    xmlns:xmp="http://ns.adobe.com/xap/1.0/"\n'
           '    xmlns:pdfx="http://ns.adobe.com/pdfx/1.3/"\n'
           '    xmlns:pdfxid="http://www.npes.org/pdfx/ns/id/">\n'
           '   <dc:format>application/pdf</dc:format>\n'
           '   <dc:title><rdf:Alt><rdf:li xml:lang="x-default">%s</rdf:li></rdf:Alt></dc:title>\n'
           '   <pdf:Producer>%s</pdf:Producer>\n'
           '   <xmp:CreatorTool>%s</xmp:CreatorTool>\n'
           '   <xmp:CreateDate>%s</xmp:CreateDate>\n'
           '   <xmp:ModifyDate>%s</xmp:ModifyDate>\n'
           '   <pdfx:PDFXStandard>PDF/X</pdfx:PDFXStandard>\n'
           '   <pdfx:PDFXVersion>%s</pdfx:PDFXVersion>\n'
           '   <pdfxid:GTS_PDFXVersion>%s</pdfxid:GTS_PDFXVersion>\n'
           '  </rdf:Description>\n'
           ' </rdf:RDF>\n'
           '</x:xmpmeta>\n'
           '<?xpacket end="w"?>\n' % (title, producer, creator, xmp_date, xmp_date,
                                      pdfx_standard, pdfx_standard))
    xmp_stream = DecodedStreamObject()
    xmp_stream.set_data(xmp.encode("utf-8"))
    xmp_stream[NameObject("/Type")] = NameObject("/Metadata")
    xmp_stream[NameObject("/Subtype")] = NameObject("/XML")
    writer._root_object[NameObject("/Metadata")] = writer._add_object(xmp_stream)

    writer.add_metadata({
        "/Title": title,
        "/Creator": creator,
        "/Producer": producer,
        "/GTS_PDFXVersion": pdfx_standard,
        "/Trapped": "/False",
        "/CreationDate": info_date,
        "/ModDate": info_date,
    })

    try:
        writer.compress_identical_objects(remove_duplicates=True, remove_unreferenced=True)
    except TypeError:
        writer.compress_identical_objects(remove_identicals=True, remove_orphans=True)
    except Exception:
        pass

    tmp = out + ".tmp"
    writer.write(tmp)
    os.replace(tmp, out)

    # output.pdf (consumed by 08/04) must follow pdfExport.fileName (07c's
    # input). Two sources of truth caused a stale gate3 binding once; keep
    # the derived field in sync here.
    if job.get("output", {}).get("pdf") != file_name:
        job.setdefault("output", {})["pdf"] = file_name
        with open(os.path.join(job_dir, "job.json"), "w", encoding="utf-8") as fh:
            json.dump(job, fh, ensure_ascii=False, indent=2)

    final = PdfReader(out)
    p = final.pages[0]
    boxes = {}
    for k in ("MediaBox", "CropBox", "TrimBox", "BleedBox", "ArtBox"):
        v = p.get("/" + k)
        if v is not None:
            boxes[k] = [round(float(x), 6) for x in v]
    report = {
        "ok": True,
        "step": "07c-finalize-pdf",
        "contract": "pdfx-standard-" + pdfx_standard.lower().replace("/", "").replace(":", "-"),
        "pdfxStandard": pdfx_standard,
        "outputConditionIdentifier": ocid,
        "fileName": file_name,
        "basePdf": "work/cmyk-final.pdf",
        "basePdfSha256": sha(base_path),
        "output": file_name,
        "outputSha256": sha(out),
        "ai": job.get("output", {}).get("ai"),
        "aiSha256": sha(os.path.join(job_dir, job["output"]["ai"])),
        "boxesPt": boxes,
        "pageMm": box_mm(boxes["MediaBox"]),
        "trimMm": box_mm(boxes["TrimBox"]),
        "bleedMm": box_mm(boxes["BleedBox"]),
        "pageCount": len(final.pages),
        "iccPath": icc_path,
        "iccSha256": icc_sha,
        "iccDefaultCMYK": True,
        "outputIntent": 1,
        "outputIntentSubtype": "GTS_PDFX",
        "marksStyle": mcfg["style"],
        "marksSource": source,
        "marksOffsetMm": float(mcfg["offsetMm"]),
        "marksOuterMm": float(mcfg["outerMm"]),
        "marksLineWidthPt": float(mcfg["lineWidthPt"]),
        "colorBars": bool(mcfg["colorBars"]),
        "note": ("PDF/X-4:2010: ICC as /DefaultCMYK + GTS_PDFX OutputIntent "
                 "with DestOutputProfile; XMP pdfxid:GTS_PDFXVersion written."),
    }
    report_path = os.path.join(job_dir, "work", "pdf-finalize.json")
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print(json.dumps({"ok": True, "output": file_name,
                      "pageMm": report["pageMm"],
                      "trimMm": report["trimMm"],
                      "bleedMm": report["bleedMm"],
                      "bytes": os.path.getsize(out)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
