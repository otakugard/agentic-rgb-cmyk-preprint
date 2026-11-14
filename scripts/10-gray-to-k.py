#!/usr/bin/env python3
"""10-gray-to-k.py <job-id> [--apply]

Authorised neutral-grey -> single-K conversion for one print job.

Why this exists
---------------
The ICC conversion turns neutral RGB greys into 4-colour CMYK greys
(e.g. 54.9/45.1/42/45.1). Small text printed in four colours is a
registration risk. The print buyer authorised replacing the neutral
grey / near-black tuples with the single-K value that best matches the
approved printed appearance (minimum CIEDE2000 under the delivery ICC).

This is an explicit exception to "convert once in 01-prepare.sh". It is
therefore never silent:

  work/cmyk-icc.pdf      the untouched ICC conversion output
  work/cmyk-k.pdf        the patched candidate
  work/konly.json        mapping, DeltaE00, replacement counts, overprint audit
  work/konly-targets.json  explicit target list (written once, then reused)

With --apply, work/cmyk.pdf is replaced by the patched file and
prepare.json keeps both hashes plus the override record. The checker
scripts are NOT changed; Gate 2 will report the deviation.

Only `k` / `K` operators whose four operands match a target within 0.02
are rewritten. Type3 CharProcs, images and all other colours are left
untouched. No channel clipping, no rounding of the K value beyond 0.1%.
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
from collections import Counter
from datetime import datetime

try:
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import ContentStream, FloatObject
except ImportError:
    print("pypdf is required: python3 -m pip install --user --break-system-packages pypdf", file=sys.stderr)
    sys.exit(78)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "lib"))
import color  # noqa: E402
import pdfprobe  # noqa: E402

TOL = 0.02
K_STEP = 0.1


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


def dump(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def best_k(tuple_cmyk, icc, intent=1):
    """K (0..100) minimising CIEDE2000 against the approved CMYK tuple."""
    ks = [round(i * K_STEP, 1) for i in range(int(100 / K_STEP) + 1)]
    lab_target = color.cmyk_to_lab([tuple_cmyk], intent=intent, src_profile=icc)[0]
    lab_k = color.cmyk_to_lab([(0, 0, 0, k) for k in ks],
                              intent=intent, src_profile=icc)
    best = None
    for k, lab in zip(ks, lab_k):
        de = color.delta_e00(lab_target, lab)
        if best is None or de < best["deltaE00"]:
            best = {"k": k, "deltaE00": de, "labK": lab}
    best["labTarget"] = lab_target
    return best


def match_tuple(values, targets):
    for t in targets:
        if all(abs(float(a) - b) <= TOL for a, b in zip(values, t["pdf"])):
            return t
    return None


def patch_pdf(src, dst, targets, reader):
    """Rewrite matching k/K operands. Returns (per-target counts, stream log)."""
    counts = Counter()
    log = []
    seen = set()

    def patch_stream(obj, label):
        try:
            stream = obj.get_object()
        except Exception:
            return
        if not hasattr(stream, "get_data"):
            return
        key = (getattr(obj, "idnum", None), getattr(obj, "generation", None), label)
        if key in seen:
            return
        seen.add(key)
        try:
            cs = ContentStream(obj, reader)
        except Exception as exc:  # noqa: BLE001
            log.append({"stream": label, "error": str(exc)})
            return
        n = 0
        ops = cs.operations
        for i, (operands, op) in enumerate(ops):
            if op in (b"k", b"K") and len(operands) == 4:
                hit = match_tuple(operands, targets)
                if hit is None:
                    continue
                k = hit["k"]
                ops[i] = ([FloatObject(0), FloatObject(0), FloatObject(0),
                           FloatObject(k / 100.0)], op)
                hit["replacements"] += 1
                counts[tuple(hit["cmyk"])] += 1
                n += 1
        if n:
            cs._data = b""
            stream.set_data(cs.get_data())
            log.append({"stream": label, "replaced": n})

    def walk_resources(res, label):
        if res is None:
            return
        res = res.get_object()
        xo = res.get("/XObject")
        if xo is None:
            return
        for name, ref in xo.get_object().items():
            obj = ref.get_object()
            if obj.get("/Subtype") == "/Form":
                patch_stream(ref, label + "/" + str(name))
                walk_resources(obj.get("/Resources"), label + "/" + str(name))

    writer = PdfWriter()
    writer.append(reader)
    for pi, page in enumerate(writer.pages):
        # page.get_contents() returns a ContentStream wrapper; writing to the
        # wrapper does not update the page's /Contents stream. Patch the raw
        # stream object (or the array entries) instead.
        contents = page.get("/Contents")
        if isinstance(contents, list):
            for ci, stream in enumerate(contents):
                patch_stream(stream, "page%d/c%d" % (pi, ci))
        else:
            patch_stream(contents, "page%d/content" % pi)
        walk_resources(page.get("/Resources"), "page%d" % pi)
    writer.write(dst)
    return counts, log


def audit_overprint(pdf):
    """PDF-level overprint audit: ExtGState flags and gs usage."""
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
    walk_stream(page.get_contents())
    walk_resources(page.get("/Resources"))

    op_true = [n for n, s in states.items() if s["OP"] in ("/true", "true")]
    op_true_lower = [n for n, s in states.items() if s["op"] in ("/true", "true")]
    opm_nonzero = [n for n, s in states.items() if s["OPM"] not in ("0", "/0")]
    return {
        "extGStateCount": len(states),
        "opTrueStates": op_true,
        "opLowerTrueStates": op_true_lower,
        "opmNonZeroStates": opm_nonzero,
        "allOverprintOff": not op_true and not op_true_lower and not opm_nonzero,
        "gsUsage": dict(sorted(usage.items())),
        "states": states,
        "note": "PDF default OP/op=false and OPM=0 when absent. All states here are /SMask-only unless listed.",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("job_id")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    job_dir = os.path.join(ROOT, "jobs", args.job_id)
    job = load(os.path.join(job_dir, "job.json"))
    spec = load(os.path.join(ROOT, "config", "print-spec.json"))
    prep_path = os.path.join(job_dir, "prepare.json")
    prep = load(prep_path)
    if not job or not spec or not prep:
        print("missing job.json / print-spec.json / prepare.json", file=sys.stderr)
        return 66

    work = os.path.join(job_dir, "work")
    targets_path = os.path.join(work, "konly-targets.json")
    base_icc = os.path.join(work, "cmyk-icc.pdf")
    current = os.path.join(work, "cmyk.pdf")
    patched = os.path.join(work, "cmyk-k.pdf")
    report_path = os.path.join(work, "konly.json")

    targets_doc = load(targets_path)
    if not targets_doc or not targets_doc.get("targets"):
        print("missing work/konly-targets.json (explicit authorised target list)", file=sys.stderr)
        return 66

    icc = (prep.get("conversion") or {}).get("targetICC") or spec["print"]["iccProfile"]
    intent = int((prep.get("conversion") or {}).get("renderingIntent", 1))

    for t in targets_doc["targets"]:
        cmyk = [float(x) for x in t["cmyk"]]
        t["cmyk"] = cmyk
        t["pdf"] = [round(x / 100.0, 4) for x in cmyk]
        best = best_k(tuple(cmyk), icc, intent=intent)
        t.update({
            "k": best["k"],
            "deltaE00": round(best["deltaE00"], 3),
            "labTarget": [round(x, 3) for x in best["labTarget"]],
            "labK": [round(x, 3) for x in best["labK"]],
            "tacBefore": round(sum(cmyk), 2),
            "tacAfter": best["k"],
            "replacements": 0,
        })

    # Always patch from the ICC original when it exists, so the script is
    # idempotent and never patches an already-patched file twice.
    source = base_icc if os.path.exists(base_icc) else current
    if not os.path.exists(source):
        print("missing work/cmyk.pdf", file=sys.stderr)
        return 66
    source_sha = sha(source)

    reader = PdfReader(source)
    counts, stream_log = patch_pdf(source, patched, targets_doc["targets"], reader)
    patched_sha = sha(patched)

    before = pdfprobe.inspect(source)
    after = pdfprobe.inspect(patched)
    after_cmyk = Counter()
    for c in after["colours"]:
        if c["space"] == "cmyk":
            after_cmyk[tuple(round(x, 2) for x in c["cmyk"])] += c.get("count", 0)

    remaining = []
    for t in targets_doc["targets"]:
        key = tuple(round(x, 2) for x in t["cmyk"])
        n = after_cmyk.get(key, 0)
        if n:
            remaining.append({"cmyk": list(key), "count": n})
        t["kTuple"] = [0.0, 0.0, 0.0, t["k"]]
        t["kCount"] = after_cmyk.get(tuple(round(x, 2) for x in t["kTuple"]), 0)

    report = {
        "ok": not remaining,
        "step": "10-gray-to-k",
        "jobId": args.job_id,
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "authorization": targets_doc.get("authorizedBy"),
        "authorizationNote": targets_doc.get("note"),
        "icc": icc,
        "iccSha256": sha(icc),
        "renderingIntent": intent,
        "source": os.path.relpath(source, job_dir),
        "sourceSha256": source_sha,
        "output": os.path.relpath(patched, job_dir),
        "outputSha256": patched_sha,
        "targets": targets_doc["targets"],
        "replacements": {"/".join("%.1f" % x for x in k): v for k, v in counts.items()},
        "streamLog": stream_log,
        "remainingTargetTuples": remaining,
        "before": {
            "colours": before["colours"],
            "overprintTrue": before["structure"].get("overprintTrue", 0),
            "opmTrue": before["structure"].get("opmTrue", 0),
        },
        "after": {
            "colours": after["colours"],
            "overprintTrue": after["structure"].get("overprintTrue", 0),
            "opmTrue": after["structure"].get("opmTrue", 0),
        },
        "overprintAudit": audit_overprint(patched),
        "note": ("Single-K values are the minimum-CIEDE2000 match under the delivery ICC. "
                 "DeltaE00 is an ICC prediction, not a press measurement."),
    }
    dump(report_path, report)

    if args.apply:
        if not report.get("ok"):
            print("validation failed: target tuples remain; work/cmyk.pdf was not replaced",
                  file=sys.stderr)
            return 65
        if os.path.exists(base_icc):
            expected = prep.get("iccOutputSha256") or prep.get("outputSha256") or ""
            if expected and sha(base_icc) != expected:
                print("work/cmyk-icc.pdf does not match prepare.outputSha256", file=sys.stderr)
                print("The preserved ICC conversion is stale. Archive it and rerun 01.", file=sys.stderr)
                return 65
        else:
            shutil.copy2(current, base_icc)
            if sha(base_icc) != (prep.get("outputSha256") or ""):
                print("WARNING: prepare.outputSha256 does not match the preserved ICC copy",
                      file=sys.stderr)
        os.replace(patched, current)
        # Keep the ICC conversion record and add the override, not replace it.
        prep_copy = os.path.join(work, "prepare-icc.json")
        if not os.path.exists(prep_copy):
            shutil.copy2(prep_path, prep_copy)
        prep = load(prep_path) or {}
        prep["iccOutput"] = "work/cmyk-icc.pdf"
        prep["iccOutputSha256"] = sha(base_icc)
        prep["output"] = "work/cmyk.pdf"
        prep["outputSha256"] = sha(current)
        prep["konlyOverride"] = {
            "authorizedBy": targets_doc.get("authorizedBy"),
            "report": "work/konly.json",
            "reportSha256": sha(report_path),
            "targets": [
                {"cmyk": t["cmyk"], "k": t["k"], "deltaE00": t["deltaE00"],
                 "replacements": t["replacements"]}
                for t in targets_doc["targets"]
            ],
            "note": "Authorised deviation from the single ICC conversion; Gate 2 control probe will report it.",
        }
        dump(prep_path, prep)

    print(json.dumps({
        "ok": report["ok"],
        "output": report["output"],
        "source": report["source"],
        "replacements": report["replacements"],
        "remaining": remaining,
        "overprint": {
            "extGState": report["overprintAudit"]["extGStateCount"],
            "opTrue": report["overprintAudit"]["opTrueStates"],
            "opmNonZero": report["overprintAudit"]["opmNonZeroStates"],
            "allOff": report["overprintAudit"]["allOverprintOff"],
        },
        "applied": bool(args.apply),
    }, ensure_ascii=False))
    return 0 if report["ok"] else 65


if __name__ == "__main__":
    sys.exit(main())
