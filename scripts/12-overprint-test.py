#!/usr/bin/env python3
"""12-overprint-test.py <job-id> <targets.json> [--name <label>]

Build a **test-only** PDF with overprint on selected near-100% K fills.
It never overwrites work/cmyk-final.pdf or the delivery PDF.

Targets JSON:
{
  "targets": [
    {
      "label": "outside heading",
      "bboxPt": [217.03, 218.68, 394.00, 236.49],
      "k": 97.8,
      "backgroundCmyk": [40, 10.6, 45.1, 5.1]
    }
  ]
}

The bbox is in final page coordinates (points, bottom-left origin). The
script matches the fill whose path bbox is within 1 pt, inserts
`q /GSK gs` around that fill, and adds /GSK with /OP true, /op true,
/OPM 1. It then reports:

  - structural overprint flags (pdfprobe)
  - predicted overprint vs knockout colour (ICC, DeltaE00)
  - TAC on the test PDF
  - whether Ghostscript's -sOverprint=simulate actually changed the pixels

`approved` is false unless the predicted DeltaE00 <= 3.0. This is a
colour-review gate, not a press measurement.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (BooleanObject, ContentStream, DictionaryObject,
                           FloatObject, NameObject, NumberObject)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "lib"))
import color  # noqa: E402
import contract as colorcontract  # noqa: E402
import pdfprobe  # noqa: E402
import raster  # noqa: E402

GS = shutil.which("gs") or "/opt/homebrew/bin/gs"
TOL = 1.0
COLOR_REVIEW_DELTA_E = 3.0


def load(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def mul(a, b):
    return [a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3],
            a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
            a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5]]


def apply(m, x, y):
    return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])


def add_gsk(page):
    res = page["/Resources"].get_object()
    egs = res.get("/ExtGState")
    if egs is None:
        egs = DictionaryObject()
        res[NameObject("/ExtGState")] = egs
    else:
        egs = egs.get_object()
    g = DictionaryObject()
    g[NameObject("/Type")] = NameObject("/ExtGState")
    g[NameObject("/OP")] = BooleanObject(True)
    g[NameObject("/op")] = BooleanObject(True)
    g[NameObject("/OPM")] = NumberObject(1)
    egs[NameObject("/GSK")] = g


def patch_fill(page, reader, target):
    cs = ContentStream(page["/Contents"], reader)
    ops = cs.operations
    st = {"ctm": [1, 0, 0, 1, 0, 0]}
    stack = []
    path = []
    fill = None
    hit = None
    for i, (operands, op) in enumerate(list(ops)):
        if op == b"q":
            stack.append({"ctm": list(st["ctm"])})
        elif op == b"Q":
            if stack:
                st = stack.pop()
        elif op == b"cm" and len(operands) == 6:
            st["ctm"] = mul([float(x) for x in operands], st["ctm"])
        elif op == b"m" and len(operands) == 2:
            path.append(apply(st["ctm"], float(operands[0]), float(operands[1])))
        elif op == b"l" and len(operands) == 2:
            path.append(apply(st["ctm"], float(operands[0]), float(operands[1])))
        elif op == b"c" and len(operands) == 6:
            for j in (0, 2, 4):
                path.append(apply(st["ctm"], float(operands[j]), float(operands[j + 1])))
        elif op in (b"v", b"y") and len(operands) == 4:
            for j in (0, 2):
                path.append(apply(st["ctm"], float(operands[j]), float(operands[j + 1])))
        elif op == b"re" and len(operands) == 4:
            x, y, w, h = [float(v) for v in operands]
            for xx, yy in ((x, y), (x + w, y), (x + w, y + h), (x, y + h)):
                path.append(apply(st["ctm"], xx, yy))
        elif op == b"k" and len(operands) == 4:
            fill = tuple(round(float(x), 3) for x in operands)
        elif op in (b"f", b"f*", b"F"):
            if fill and path:
                bb = (min(p[0] for p in path), min(p[1] for p in path),
                      max(p[0] for p in path), max(p[1] for p in path))
                if abs(fill[3] - float(target["k"]) / 100.0) < 0.002 and all(
                        abs(fill[j]) < 0.002 for j in range(3)):
                    if all(abs(bb[j] - target["bboxPt"][j]) <= TOL for j in range(4)):
                        hit = (i, bb, fill)
                        break
            path = []
            fill = None
        elif op in (b"n", b"S", b"s"):
            path = []
            fill = None
    if hit is None:
        raise RuntimeError("no K fill matched bbox %r" % (target["bboxPt"],))
    i, bb, fill = hit
    insert = [([], b"q"), ([NameObject("/GSK")], b"gs")]
    if target.get("fillCmyk"):
        vals = [FloatObject(float(x) / 100.0) for x in target["fillCmyk"]]
        insert.append((vals, b"k"))
    ops[i:i] = insert
    ops[i + len(insert) + 1:i + len(insert) + 1] = [([], b"Q")]
    cs._data = b""
    page["/Contents"].get_object().set_data(cs.get_data())
    used = target.get("fillCmyk") or [0.0, 0.0, 0.0, float(target["k"])]
    return {"bboxPt": [round(x, 3) for x in bb],
            "originalK": round(fill[3] * 100, 2),
            "fillCmyk": [round(float(x), 2) for x in used]}


def predicted(target):
    k = float(target["k"])
    bg = [float(x) for x in target["backgroundCmyk"]]
    fill = [float(x) for x in (target.get("fillCmyk") or [0.0, 0.0, 0.0, k])]
    ko = (0.0, 0.0, 0.0, k)
    # OPM 1: a non-zero source channel overprints; a zero channel leaves the
    # background channel in place. K uses the source K when non-zero.
    op = tuple(fill[i] if fill[i] > 0 else bg[i] for i in range(3)) + (
        fill[3] if fill[3] > 0 else bg[3],)
    icc = target["_icc"]
    lab_ko = color.cmyk_to_lab([ko], intent=1, src_profile=icc)[0]
    lab_op = color.cmyk_to_lab([op], intent=1, src_profile=icc)[0]
    return {
        "knockoutCmyk": list(ko),
        "overprintCmyk": list(op),
        "knockoutLab": [round(x, 3) for x in lab_ko],
        "overprintLab": [round(x, 3) for x in lab_op],
        "deltaE00": round(color.delta_e00(lab_ko, lab_op), 3),
        "tacKnockout": round(sum(ko), 2),
        "tacOverprint": round(sum(op), 2),
    }


def tac(pdf):
    with tempfile.TemporaryDirectory() as tmp:
        tif = os.path.join(tmp, "p.tif")
        subprocess.run([GS, "-q", "-dBATCH", "-dNOPAUSE", "-dSAFER",
                        "-dUseFastColor=true", "-sDEVICE=tiff32nc", "-r300",
                        "-o", tif, pdf], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        a = raster.read_tiff(tif)["data"].astype(np.int16)
    t = a.sum(axis=2) * 100.0 / 255.0
    return {"maxTacPercent": round(float(t.max()), 2),
            "shareOver300Percent": round(float((t > 300).mean() * 100), 3)}


def render_diff(base, test, bbox, icc):
    dpi = 150
    sc = dpi / 72.0
    with tempfile.TemporaryDirectory() as tmp:
        arrs = []
        for tag, pdf in (("base", base), ("test", test)):
            tif = os.path.join(tmp, tag + ".tif")
            subprocess.run([GS, "-q", "-dBATCH", "-dNOPAUSE", "-dSAFER",
                            "-sOverprint=simulate", "-sDEVICE=tiff32nc",
                            "-sDefaultCMYKProfile=" + icc,
                            "-sOutputICCProfile=" + icc,
                            "-r%d" % dpi, "-o", tif, pdf], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            arrs.append(raster.read_tiff(tif)["data"][..., :4].astype(np.int16))
    a, b = arrs
    h = min(a.shape[0], b.shape[0])
    w = min(a.shape[1], b.shape[1])
    a, b = a[:h, :w], b[:h, :w]
    x0, y0, x1, y1 = bbox
    page_h = h / sc if sc else 0.0
    X0, X1 = max(0, int(x0 * sc)), min(w, int(x1 * sc))
    Y0, Y1 = max(0, int((page_h - y1) * sc)), min(h, int((page_h - y0) * sc))
    if Y0 >= Y1 or X0 >= X1:
        return {"cropPx": [X0, Y0, X1, Y1], "maxChannelDiff": None,
                "changedPixels": None, "note": "bbox outside the rendered page"}
    ca, cb = a[Y0:Y1, X0:X1], b[Y0:Y1, X0:X1]
    d = np.abs(ca - cb)
    return {
        "cropPx": [X0, Y0, X1, Y1],
        "maxChannelDiff": int(d.max()),
        "changedPixels": int((d.max(axis=2) > 2).sum()),
        "totalPixels": int(d.shape[0] * d.shape[1]),
        "note": ("0 changed pixels means Ghostscript's overprint simulation did not "
                 "visualise this fill (complex clip/transparency); Acrobat is required."
                 if int((d.max(axis=2) > 2).sum()) == 0 else
                 "pixels changed under -sOverprint=simulate"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("job_id")
    ap.add_argument("targets_json")
    ap.add_argument("--name", default="overprint-test")
    ap.add_argument("--apply", action="store_true",
                    help="after writing the test PDF, replace work/cmyk-final.pdf (with backup)")
    ap.add_argument("--accept-color-shift", action="store_true",
                    help="allow --apply even when the colour review fails")
    args = ap.parse_args()

    job_dir = os.path.join(ROOT, "jobs", args.job_id)
    job = load(os.path.join(job_dir, "job.json"))
    spec = load(os.path.join(ROOT, "config", "print-spec.json"))
    global GS
    _gs = ((spec or {}).get("ghostscript") or {}).get("binary") or "gs"
    GS = shutil.which(_gs) or _gs
    if not job:
        print("job.json not found", file=sys.stderr)
        return 66
    icc = colorcontract.profile(colorcontract.load(), "target")[0]
    base = os.path.join(job_dir, "work", "cmyk-final.pdf")
    targets_doc = load(args.targets_json)
    if not targets_doc or not targets_doc.get("targets"):
        print("targets file must contain a non-empty targets list", file=sys.stderr)
        return 66

    out_dir = os.path.join(job_dir, "work", "overprint-test")
    os.makedirs(out_dir, exist_ok=True)

    r = PdfReader(base)
    w = PdfWriter()
    w.append(r)
    page = w.pages[0]
    add_gsk(page)
    results = []
    for t in targets_doc["targets"]:
        t["_icc"] = icc
        hit = patch_fill(page, r, t)
        pred = predicted(t)
        results.append({"label": t["label"], "hit": hit, "predicted": pred})

    test_pdf = os.path.join(out_dir, args.name + "-op.pdf")
    w.write(test_pdf)

    info = pdfprobe.inspect(test_pdf)
    report = {
        "ok": True,
        "step": "12-overprint-test",
        "jobId": args.job_id,
        "name": args.name,
        "basePdf": "work/cmyk-final.pdf",
        "basePdfSha256": sha(base),
        "testPdf": os.path.relpath(test_pdf, job_dir),
        "testPdfSha256": sha(test_pdf),
        "appliedToDelivery": False,
        "targets": results,
        "structure": {
            "overprintTrue": info["structure"].get("overprintTrue", 0),
            "opmTrue": info["structure"].get("opmTrue", 0),
            "separations": info["structure"].get("separationNames", []),
            "allSeparation": info["structure"].get("allSeparation", False),
        },
        "tac": tac(test_pdf),
        "render": render_diff(base, test_pdf, targets_doc["targets"][0]["bboxPt"], icc),
        "colorReview": {
            "thresholdDeltaE00": COLOR_REVIEW_DELTA_E,
            "worstDeltaE00": max(x["predicted"]["deltaE00"] for x in results),
            "pass": all(x["predicted"]["deltaE00"] <= COLOR_REVIEW_DELTA_E for x in results),
            "note": "ICC-predicted overprint vs knockout; not a press measurement.",
        },
        "note": ("Test-only PDF. Delivery PDF and work/cmyk-final.pdf were not modified."),
    }
    if args.apply:
        if not report["colorReview"]["pass"] and not args.accept_color_shift:
            print("colour review failed; pass --accept-color-shift to apply anyway", file=sys.stderr)
            return 65
        backup = os.path.join(job_dir, "work", "cmyk-final-pre-op.pdf")
        if not os.path.exists(backup):
            shutil.copy2(base, backup)
        shutil.copy2(test_pdf, base)
        report["appliedToDelivery"] = True
        report["appliedTo"] = "work/cmyk-final.pdf"
        report["backup"] = os.path.relpath(backup, job_dir)
        report["backupSha256"] = sha(backup)
        report["acceptedColorShift"] = (not report["colorReview"]["pass"])
        report["note"] = ("Applied to work/cmyk-final.pdf; the delivery PDF must be "
                          "regenerated with 07c and re-gated with 08/04.")

    report_path = os.path.join(out_dir, args.name + ".json")
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)

    print(json.dumps({
        "ok": True,
        "testPdf": report["testPdf"],
        "targets": [{"label": x["label"], "hit": x["hit"],
                     "deltaE00": x["predicted"]["deltaE00"],
                     "tacKnockout": x["predicted"]["tacKnockout"],
                     "tacOverprint": x["predicted"]["tacOverprint"]} for x in results],
        "structure": report["structure"],
        "tac": report["tac"],
        "render": report["render"],
        "colorReview": report["colorReview"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
