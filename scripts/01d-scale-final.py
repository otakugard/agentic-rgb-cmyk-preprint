#!/usr/bin/env python3
"""01d-scale-final.py <job-id>

Scale work/cmyk.pdf from Figma's 1px=1pt geometry to the final bleed page
(trim + 2 x bleed) and write work/cmyk-final.pdf.

This exists for documents where Illustrator's group/resize path corrupts
clipping groups (for example, a single sheet made of two panel frames).

Geometry is applied as a page CTM with pypdf, not a second Ghostscript
pass: the CMYK operators and their exact values stay untouched. 02 opens
work/cmyk-final.pdf and skips group/resize when it sees 100%.

The original work/cmyk.pdf stays at the Figma page size for 05/06/07.
"""

import hashlib
import json
import os
import re
import sys

try:
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import ArrayObject, DecodedStreamObject, FloatObject, NameObject
except ImportError:
    print("pypdf is required: python3 -m pip install --user --break-system-packages pypdf", file=sys.stderr)
    sys.exit(78)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def load(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def page_mm(raw):
    mb = re.search(rb"/MediaBox\s*\[([^\]]*)\]", raw)
    if not mb:
        return None
    nums = [float(x) for x in mb.group(1).split()]
    if len(nums) != 4:
        return None
    return [round((nums[2] - nums[0]) * 25.4 / 72, 2),
            round((nums[3] - nums[1]) * 25.4 / 72, 2)]


def main():
    if len(sys.argv) != 2:
        print("usage: 01d-scale-final.py <job-id>", file=sys.stderr)
        return 64
    job_id = sys.argv[1]
    job_dir = os.path.join(ROOT, "jobs", job_id)
    job = load(os.path.join(job_dir, "job.json"))
    spec = load(os.path.join(ROOT, "config", "print-spec.json"))
    if not job or not spec:
        print("missing job.json or print-spec.json", file=sys.stderr)
        return 66
    trim = job.get("trim") or {}
    if trim.get("w") and trim.get("h"):
        wmm, hmm = float(trim["w"]), float(trim["h"])
    elif trim.get("preset"):
        wmm, hmm = [float(x) for x in spec["trimPresetsMm"][trim["preset"]]]
    else:
        print("job.trim cannot be resolved", file=sys.stderr)
        return 66
    bleed = float(job.get("bleedMm", spec["print"].get("bleedMm", 0)) or 0)
    target_w = wmm + 2 * bleed
    target_h = hmm + 2 * bleed
    source_mode = str((job.get("source") or {}).get("kind") or "figma").strip().lower()

    src = os.path.join(job_dir, "work", "cmyk.pdf")
    dst = os.path.join(job_dir, "work", "cmyk-final.pdf")
    report_path = os.path.join(job_dir, "work", "scale-final.json")
    if not os.path.exists(src):
        print("missing work/cmyk.pdf", file=sys.stderr)
        return 66
    if os.path.realpath(src) == os.path.realpath(dst):
        print("input and output are the same path", file=sys.stderr)
        return 65

    src_raw = open(src, "rb").read()
    src_mm = page_mm(src_raw)
    reader = PdfReader(src)
    writer = PdfWriter()
    writer.append(reader)
    page = writer.pages[0]
    mb = [float(x) for x in page["/MediaBox"]]
    src_w_pt = mb[2] - mb[0]
    src_h_pt = mb[3] - mb[1]
    if src_w_pt <= 0 or src_h_pt <= 0:
        print("invalid source MediaBox", file=sys.stderr)
        return 65
    target_w_pt = target_w * 72 / 25.4
    target_h_pt = target_h * 72 / 25.4
    sx = target_w_pt / src_w_pt
    sy = target_h_pt / src_h_pt
    if abs(sx - sy) > 0.0002:
        print("non-uniform scale: sx=%.6f sy=%.6f" % (sx, sy), file=sys.stderr)
        return 65
    if source_mode == "srgb-pdf" and abs(sx - 1.0) > 0.005:
        print("source.kind=srgb-pdf expects a final-size PDF at %.1f x %.1f mm; got %.1f x %.1f mm (scale %.4f)"
              % (target_w, target_h, src_w_pt * 25.4 / 72, src_h_pt * 25.4 / 72, sx),
              file=sys.stderr)
        print("Resize the source deliberately in the layout tool and start a new job. The pipeline does not auto-fit an existing PDF.",
              file=sys.stderr)
        return 65

    data = page.get_contents().get_data()
    stream = DecodedStreamObject()
    stream.set_data(("q %.8f 0 0 %.8f 0 0 cm\n" % (sx, sx)).encode("ascii") + data + b"\nQ\n")
    page[NameObject("/Contents")] = writer._add_object(stream)
    box = ArrayObject([FloatObject(0), FloatObject(0),
                       FloatObject(target_w_pt), FloatObject(target_h_pt)])
    for key in ("/MediaBox", "/CropBox", "/TrimBox", "/BleedBox"):
        page[NameObject(key)] = box

    tmp = dst + ".tmp"
    writer.write(tmp)
    os.replace(tmp, dst)

    dst_raw = open(dst, "rb").read()
    out_mm = page_mm(dst_raw)
    if out_mm is None or abs(out_mm[0] - target_w) > 0.2 or abs(out_mm[1] - target_h) > 0.2:
        print("scaled page is %s mm, expected %.2f x %.2f" % (out_mm, target_w, target_h), file=sys.stderr)
        return 65

    report = {
        "ok": True,
        "step": "01d-scale-final",
        "method": "pypdf-page-ctm",
        "input": os.path.relpath(src, job_dir),
        "inputSha256": hashlib.sha256(src_raw).hexdigest(),
        "output": os.path.relpath(dst, job_dir),
        "outputSha256": hashlib.sha256(dst_raw).hexdigest(),
        "inputPageMm": src_mm,
        "outputPageMm": out_mm,
        "targetPageMm": [round(target_w, 3), round(target_h, 3)],
        "targetPt": [round(target_w_pt, 4), round(target_h_pt, 4)],
        "scale": round(sx, 8),
        "sourceMode": source_mode,
        "note": "Geometry-only page CTM; CMYK operators are not re-encoded.",
    }
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)

    prep_path = os.path.join(job_dir, "prepare.json")
    prep = load(prep_path, {}) or {}
    prep.update({
        "finalPdf": report["output"],
        "finalPdfSha256": report["outputSha256"],
        "finalPageMm": out_mm,
        "scaleInPdf": True,
        "scaleFinalSha256": hashlib.sha256(open(report_path, "rb").read()).hexdigest(),
    })
    with open(prep_path, "w", encoding="utf-8") as fh:
        json.dump(prep, fh, ensure_ascii=False, indent=2)

    print(json.dumps({"ok": True, "output": report["output"],
                      "pageMm": out_mm, "bytes": len(dst_raw)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
