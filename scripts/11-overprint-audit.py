#!/usr/bin/env python3
"""11-overprint-audit.py <job-id>

Read-only overprint audit for the final PDF plus the .ai object audit.

It answers three separate questions:

  1. Does the PDF carry any /OP, /op or /OPM in its ExtGState resources?
  2. Do the content streams actually select those states?
  3. Do the .ai objects have fillOverprint set?

It never changes overprint. A clean audit (all off) is reported as
"knockout", not as "pass": whether K-only text should overprint depends
on the printer's workflow and must be verified in Acrobat Output Preview.
"""

import hashlib
import json
import os
import sys
from collections import Counter

from pypdf import PdfReader
from pypdf.generic import ContentStream

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "lib"))
import pdfprobe  # noqa: E402


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


def audit_pdf(pdf):
    reader = PdfReader(pdf)
    states = {}
    usage = Counter()
    seen = set()

    def walk_stream(obj):
        key = getattr(obj, "idnum", None)
        if key is not None and key in seen:
            return
        if key is not None:
            seen.add(key)
        try:
            cs = ContentStream(obj, reader)
        except Exception:
            return
        for operands, op in cs.operations:
            if op == b"gs" and len(operands) == 1:
                usage[str(operands[0])] += 1

    def walk_resources(res):
        if res is None:
            return
        res = res.get_object()
        egs = res.get("/ExtGState")
        if egs:
            for name, ref in egs.get_object().items():
                d = ref.get_object()
                states[str(name)] = {
                    "OP": str(d.get("/OP", "/false")).lower(),
                    "op": str(d.get("/op", "/false")).lower(),
                    "OPM": str(d.get("/OPM", "0")),
                    "BM": str(d.get("/BM", "/Normal")),
                    "hasSMask": "/SMask" in d,
                    "otherKeys": sorted(str(k) for k in d.keys()
                                        if str(k) not in ("/Type", "/SMask", "/OP", "/op", "/OPM", "/BM")),
                }
        xo = res.get("/XObject")
        if xo:
            for name, ref in xo.get_object().items():
                obj = ref.get_object()
                if obj.get("/Subtype") == "/Form":
                    walk_stream(ref)
                    walk_resources(obj.get("/Resources"))

    page = reader.pages[0]
    walk_stream(page.get("/Contents"))
    walk_resources(page.get("/Resources"))

    op_true = sorted(n for n, s in states.items() if s["OP"] in ("/true", "true"))
    op_lower_true = sorted(n for n, s in states.items() if s["op"] in ("/true", "true"))
    opm_nonzero = sorted(n for n, s in states.items() if s["OPM"] not in ("0", "/0"))
    info = pdfprobe.inspect(pdf)
    return {
        "extGStateCount": len(states),
        "states": states,
        "gsUsage": dict(sorted(usage.items())),
        "opTrueStates": op_true,
        "opLowerTrueStates": op_lower_true,
        "opmNonZeroStates": opm_nonzero,
        "allOverprintOff": not op_true and not op_lower_true and not opm_nonzero,
        "contentOverprintTrue": info["structure"].get("overprintTrue", 0),
        "contentOpmTrue": info["structure"].get("opmTrue", 0),
        "note": ("PDF default OP/op=false and OPM=0 when absent. "
                 "All states here are /SMask-only unless listed."),
    }


def main():
    if len(sys.argv) != 2:
        print("usage: 11-overprint-audit.py <job-id>", file=sys.stderr)
        return 64
    job_id = sys.argv[1]
    job_dir = os.path.join(ROOT, "jobs", job_id)
    job = load(os.path.join(job_dir, "job.json"))
    if not job:
        print("job.json not found", file=sys.stderr)
        return 66

    final_pdf = os.path.join(job_dir, job["output"]["pdf"])
    ai = os.path.join(job_dir, job["output"]["ai"])
    if not os.path.exists(final_pdf):
        print("final PDF missing: %s" % final_pdf, file=sys.stderr)
        return 66

    pdf_audit = audit_pdf(final_pdf)
    ai_path = os.path.join(job_dir, "work", "ai-color-audit.json")
    ai_audit = load(ai_path, {}) or {}
    ai_available = os.path.exists(ai_path) and bool(ai_audit.get("colors"))
    gate3 = load(os.path.join(job_dir, "gate3.json"), {}) or {}

    ai_colors = ai_audit.get("colors") or {}
    ai_filled = sum(v.get("count", 0) for v in ai_colors.values())
    ai_op_true = sum(v.get("fillOverprintTrue", 0) for v in ai_colors.values())
    ai_op_false = sum(v.get("fillOverprintFalse", 0) for v in ai_colors.values())
    k_only = {k: v for k, v in ai_colors.items()
              if v.get("cmyk") and sum(v["cmyk"][:3]) < 1.0}

    g3_over = next((c for c in (gate3.get("checks") or [])
                    if c.get("id") == "pdf.overprint"), None)

    all_off = (ai_available
               and pdf_audit["allOverprintOff"]
               and ai_op_true == 0
               and pdf_audit["contentOverprintTrue"] == 0
               and pdf_audit["contentOpmTrue"] == 0)
    evidence_gap = None if ai_available else (
        "work/ai-color-audit.json is missing; the AI object-level overprint state "
        "was not inspected. Do not report a full knockout result.")

    report = {
        "ok": True,
        "step": "11-overprint-audit",
        "jobId": job_id,
        "generatedAt": __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds"),
        "finalPdf": os.path.basename(final_pdf),
        "finalPdfSha256": sha(final_pdf),
        "ai": os.path.basename(ai),
        "aiSha256": sha(ai) if os.path.exists(ai) else None,
        "pdf": pdf_audit,
        "ai": {
            "available": ai_available,
            "source": ai_audit.get("source"),
            "filledObjects": ai_filled,
            "fillOverprintTrue": ai_op_true,
            "fillOverprintFalse": ai_op_false,
            "kOnlyObjects": sum(v.get("count", 0) for v in k_only.values()),
            "kOnlyColors": k_only,
        },
        "gate3Overprint": g3_over,
        "allOverprintOff": all_off,
        "evidenceGap": evidence_gap,
        "conclusion": ("all fills are knockout (overprint off); K-only text is knockout"
                       if all_off else
                       "overprint/OPM is present; requires per-object review"
                       if (not ai_available
                           and (pdf_audit["overprintTrue"] or pdf_audit["opmTrue"]
                                or pdf_audit["contentOverprintTrue"]
                                or pdf_audit["contentOpmTrue"])) else
                       "PDF knockout only; AI object evidence missing"
                       if not ai_available else
                       "overprint/OPM is present; requires per-object review"),
        "risk": ("K-only text on a tinted background can show a light halo if the K plate "
                 "misregisters. Setting K text to overprint is one remedy, but it changes "
                 "rendering and must be verified in Acrobat Output Preview / with the printer."),
        "applied": False,
        "note": ("Audit only. No overprint was added or removed. "
                 "Acrobat Output Preview remains an external check."),
    }
    out = os.path.join(job_dir, "work", "overprint-audit.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)

    print(json.dumps({
        "ok": True,
        "pdf": {"extGState": pdf_audit["extGStateCount"],
                "opTrue": pdf_audit["opTrueStates"],
                "opmNonZero": pdf_audit["opmNonZeroStates"],
                "allOff": pdf_audit["allOverprintOff"],
                "contentOverprint": pdf_audit["contentOverprintTrue"]},
        "ai": {"filled": ai_filled, "fillOverprintTrue": ai_op_true,
               "fillOverprintFalse": ai_op_false, "kOnlyObjects": report["ai"]["kOnlyObjects"]},
        "allOverprintOff": all_off,
        "evidenceGap": evidence_gap,
        "conclusion": report["conclusion"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
