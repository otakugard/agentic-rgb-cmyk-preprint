#!/usr/bin/env python3
"""06-color-audit.py <job-id>

Build a semantic sRGB -> FOGRA39 CMYK map from the actual source colours
and the actual Ghostscript conversion path.

All Lab / ΔE00 values are ICC predictions (LittleCMS), not printed
measurements. Brand tolerance is not asserted unless the print buyer
confirms it.
"""

import hashlib
import json
import os
import shutil
import sys
from collections import Counter
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "lib"))
import color  # noqa: E402
import pdfprobe  # noqa: E402


def load(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def stable_icc(job_dir, prepare):
    """Copy the source ICC to a path keyed only by its content hash."""
    src = os.path.join(job_dir, "work", "source-icc.icc")
    sha = prepare.get("sourceICCsha256")
    if not sha or not os.path.exists(src):
        return src, sha
    dst = os.path.join(ROOT, "color", "profiles", "srgb-%s.icc" % sha[:16])
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if not os.path.exists(dst) or hashlib.sha256(open(dst, "rb").read()).hexdigest() != sha:
        shutil.copy2(src, dst)
    return dst, sha


def main():
    if len(sys.argv) != 2:
        print("usage: 06-color-audit.py <job-id>", file=sys.stderr)
        return 64
    job_id = sys.argv[1]
    job_dir = os.path.join(ROOT, "jobs", job_id)
    prepare = load(os.path.join(job_dir, "prepare.json"))
    audit = load(os.path.join(job_dir, "work", "source-audit.json"))
    job = load(os.path.join(job_dir, "job.json"), {}) or {}
    if not prepare or not audit:
        print("missing prepare.json or source-audit.json; run 01-prepare first", file=sys.stderr)
        return 66

    conv = prepare["conversion"]
    _gs = conv.get("gsBinary") or "gs"
    color.GS = shutil.which(_gs) or _gs
    icc_path, icc_sha = stable_icc(job_dir, prepare)
    target_path = conv["targetICC"]
    target_sha = conv["targetICCsha256"]
    probe_args = list(conv["args"])
    # The cache must not depend on the job path. The ICC bytes are identical.
    fixed = []
    for a in probe_args:
        if a.startswith("-sDefaultRGBProfile=") and a.endswith("/source-icc.icc"):
            a = "-sDefaultRGBProfile=" + icc_path
        elif a.startswith("--permit-file-read=") and a.endswith("/source-icc.icc"):
            a = "--permit-file-read=" + icc_path
        fixed.append(a)
    probe_args = fixed

    brand_map = load(os.path.join(ROOT, "color", "brand-map.json"), {}) or {}
    names = brand_map.get("colors", {})
    tolerance = brand_map.get("toleranceDeltaE00")

    palette = []
    seen = set()
    for c in audit.get("colours", []):
        if c.get("space") != "rgb" or not c.get("hex"):
            continue
        if c["hex"] in seen:
            continue
        seen.add(c["hex"])
        palette.append(c)
    palette.sort(key=lambda c: -c.get("count", 0))

    rows = []
    cache = os.path.join(ROOT, "color", "probe-cache.json")

    # The authoritative CMYK values are the ones actually written to cmyk.pdf.
    # The patch probe below is a control experiment for the same command path;
    # it can differ in the last 0.1% because the source PDF stores float values.
    pdf_info = pdfprobe.inspect(os.path.join(job_dir, prepare["output"]))
    pdf_cmyks = [tuple(c["cmyk"]) for c in pdf_info["colours"] if c["space"] == "cmyk"]
    pdf_labs = (color.cmyk_to_lab(pdf_cmyks, intent=1, src_profile=target_path)
                if pdf_cmyks else [])
    used = set()
    # Authorised single-K override (job prepare.json -> konlyOverride).
    # The source colour still predicts the ICC tuple; the file contains the
    # recorded K tuple. Match it explicitly instead of letting the greedy
    # CMYK-distance matcher pair a grey with an unrelated chromatic tuple.
    override_targets = {}
    for t in (prepare.get("konlyOverride") or {}).get("targets", []):
        tgt = tuple(round(float(x), 2) for x in t["cmyk"])
        override_targets[tgt] = (0.0, 0.0, 0.0, round(float(t["k"]), 2))
    override_k_tuples = set(override_targets.values())
    ai_hist = load(os.path.join(job_dir, "result.json"), {}).get("cmykHistogram", {}) or {}
    ai_path = os.path.join(job_dir, (job.get("output") or {}).get("ai", ""))
    ai_counts = Counter()
    if os.path.exists(ai_path):
        ai_info = pdfprobe.inspect(ai_path)
        for c in ai_info["colours"]:
            if c["space"] == "cmyk":
                ai_counts[tuple(round(x, 2) for x in c["cmyk"])] += c.get("count", 0)

    for c in palette:
        rgb = tuple(c["rgb"])
        probe = color.actual_cmyk(probe_args, rgb, cache)
        pred = color.rgb_to_cmyk(icc_path, [rgb], intent=conv["renderingIntent"],
                                 bpc=conv["blackPointCompensation"],
                                 dst_profile=target_path)[0]
        src_lab = color.rgb_to_lab(icc_path, [rgb], intent=1)[0]
        probe_lab = color.cmyk_to_lab([tuple(probe["cmyk"])], intent=1,
                                      src_profile=target_path)[0]
        pred_lab = color.cmyk_to_lab([tuple(pred)], intent=1,
                                     src_profile=target_path)[0]

        override_k = None
        for tgt, kt in override_targets.items():
            if sum(abs(a - b) for a, b in zip(pred, tgt)) <= 1.0:
                override_k = kt
                break

        # Greedy nearest match to a CMYK tuple actually present in cmyk.pdf.
        best = None
        for idx, cand in enumerate(pdf_cmyks):
            if override_k is not None:
                # The authorised override is matched by its recorded K value.
                if not all(abs(a - b) <= 0.2 for a, b in zip(cand, override_k)):
                    continue
            elif tuple(round(float(x), 2) for x in cand) in override_k_tuples:
                # Do not let a chromatic source grab another colour's K tuple.
                continue
            # Match on the CMYK values produced by the same CMM, not on
            # source Lab: two near-blacks can be closer in Lab than in ink.
            cmyk_dist = sum(abs(a - b) for a, b in zip(cand, pred))
            de_match = color.delta_e00(src_lab, pdf_labs[idx])
            score = cmyk_dist + (1000 if idx in used else 0)
            if best is None or score < best[0]:
                best = (score, idx, de_match)
        if best is None:
            # Do not substitute the prediction for the actual file value.
            # An unmatched source colour is a real evidence gap.
            pdf_cmyk = None
            pdf_lab = None
            match_de = None
            pdf_count = 0
            de = de_cmm = de_probe = None
            ai_key = None
        else:
            _, idx, match_de = best
            used.add(idx)
            pdf_cmyk = pdf_cmyks[idx]
            pdf_lab = pdf_labs[idx]
            pdf_count = next((x["count"] for x in pdf_info["colours"]
                              if x["space"] == "cmyk" and tuple(x["cmyk"]) == pdf_cmyk), 0)
            de = color.delta_e00(src_lab, pdf_lab)
            # Independent check: LittleCMS prediction vs the CMYK actually written.
            de_cmm = color.delta_e00(pred_lab, pdf_lab)
            de_probe = color.delta_e00(probe_lab, pdf_lab)
            def _fmt(v):
                t = "%.1f" % v
                return t[:-2] if t.endswith(".0") else t
            ai_key = "/".join(_fmt(x) for x in pdf_cmyk)
        entry = names.get(c["hex"], {})
        rows.append({
            "hex": c["hex"],
            "rgb": list(rgb),
            "name": entry.get("name") or "descriptive",
            "nameSource": entry.get("nameSource") or "descriptive; no brand token read",
            "role": entry.get("role") or "descriptive",
            "sourceOperators": c.get("count", 0),
            "pdfOperators": pdf_count,
            "aiOperators": (next((n for k, n in ai_counts.items()
                                   if pdf_cmyk and sum(abs(a - b) for a, b in zip(k, pdf_cmyk)) <= 0.3), None)
                             if pdf_cmyk else None),
            "sourceLab": color.round_lab(src_lab),
            "predictedCmyk": [round(x, 2) for x in pred],
            "actualCmyk": [round(x, 2) for x in pdf_cmyk] if pdf_cmyk else None,
            "controlProbeCmyk": [round(x, 2) for x in probe["cmyk"]],
            "actualLab": color.round_lab(pdf_lab) if pdf_lab else None,
            "matched": pdf_cmyk is not None,
            "override": override_k is not None and pdf_cmyk is not None,
            "overrideK": list(override_k) if override_k is not None else None,
            "matchDeltaE00": round(match_de, 4) if match_de is not None else None,
            "deltaE00Predicted": round(de, 3) if de is not None else None,
            "deltaE00CmmDifference": round(de_cmm, 3) if de_cmm is not None else None,
            "deltaE00ProbeVsPdf": round(de_probe, 3) if de_probe is not None else None,
            "tacPercent": round(sum(pdf_cmyk), 2) if pdf_cmyk else None,
            "probeCommandSha256": hashlib.sha256(probe["command"].encode()).hexdigest(),
        })

    values = sorted(r["deltaE00Predicted"] for r in rows if r["deltaE00Predicted"] is not None)
    def pct(p):
        if not values:
            return None
        i = min(len(values) - 1, max(0, int(round((len(values) - 1) * p / 100.0))))
        return values[i]

    payload = {
        "jobId": job_id,
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "source": {
            "sha256": prepare.get("inputSha256"),
            "icc": prepare.get("sourceICC"),
            "iccSha256": icc_sha,
            "kind": prepare.get("sourceKind"),
        },
        "cmykSha256": prepare.get("outputSha256"),
        "brandMapSha256": hashlib.sha256(open(os.path.join(ROOT, "color", "brand-map.json"), "rb").read()).hexdigest(),
        "target": {
            "icc": os.path.basename(target_path),
            "iccSha256": target_sha,
            "condition": "Coated FOGRA39 (ISO 12647-2:2004)",
        },
        "conversion": {
            "renderingIntent": conv["renderingIntent"],
            "renderingIntentName": conv["renderingIntentName"],
            "blackPointCompensation": conv["blackPointCompensation"],
            "overrideICC": conv["overrideICC"],
            "cmm": "LittleCMS (transicc)",
            "labReference": "D50, 2-degree observer, ICC PCS",
            "note": "ICC prediction, not a physical measurement.",
        },
        "tolerance": {
            "deltaE00": tolerance,
            "status": "pending" if tolerance is None else "confirmed",
            "note": "No brand tolerance is asserted without buyer confirmation." if tolerance is None else "",
        },
        "summary": {
            "colours": len(rows),
            "maxDeltaE00": max(values) if values else None,
            "p50DeltaE00": pct(50),
            "p90DeltaE00": pct(90),
            "over2": sum(1 for v in values if v > 2),
            "over5": sum(1 for v in values if v > 5),
            "over10": sum(1 for v in values if v > 10),
            "maxCmmDifference": max((r["deltaE00CmmDifference"] for r in rows
                                     if r["deltaE00CmmDifference"] is not None), default=None),
            "maxProbeVsPdf": max((r["deltaE00ProbeVsPdf"] for r in rows if r["deltaE00ProbeVsPdf"] is not None), default=None),
            "unmatched": [r["hex"] for r in rows if not r["matched"]],
        },
        "rows": rows,
        "limitations": [
            "ΔE00 is predicted from ICC transforms, not measured on paper.",
            "The CMYK values come from the same Ghostscript command path on control patches.",
            "Illustrator Proof Setup and contract proof are separate, still-pending checks.",
        ],
    }

    out = os.path.join(job_dir, "color-audit.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)

    lines = [
        "# Color Mapping — %s" % job_id,
        "",
        "sRGB: `%s` (%s) · CMYK: `Coated FOGRA39` (%s)" % (
            (prepare.get("sourceICC") or {}).get("description", "?"),
            icc_sha[:16], target_sha[:16]),
        "",
        "转换：%s intent %d, BPC %s, OverrideICC %s。色差为 **ICC 预测值，非实物测量**。" % (
            conv["renderingIntentName"], conv["renderingIntent"],
            conv["blackPointCompensation"], conv["overrideICC"]),
        "",
        "| 语义 | HEX | RGB | FOGRA39 CMYK | TAC | 预测 ΔE00 | 控制探针差 | PDF/AI 次数 | 命名依据 |",
        "|---|---|---|---|---:|---:|---:|---:|---|",
    ]
    for r in rows:
        ai_count = "?" if r.get("aiOperators") is None else str(r["aiOperators"])
        cmyk_text = (" / ".join("%.1f" % x for x in r["actualCmyk"])
                     if r.get("actualCmyk") else "未匹配")
        tac_text = "%.1f%%" % r["tacPercent"] if r.get("tacPercent") is not None else "—"
        de_text = "%.2f" % r["deltaE00Predicted"] if r.get("deltaE00Predicted") is not None else "—"
        cmm_text = "%.2f" % r["deltaE00CmmDifference"] if r.get("deltaE00CmmDifference") is not None else "—"
        lines.append("| %s | %s | %s | %s | %s | %s | %s | %d / %s | %s |" % (
            r["name"] or "未命名", r["hex"], "/".join(str(x) for x in r["rgb"]),
            cmyk_text, tac_text, de_text, cmm_text, r["pdfOperators"],
            ai_count, r["nameSource"] or "未确认"))
    if payload["tolerance"]["status"] == "pending":
        lines += ["", "品牌色容差未确认：上表 ΔE00 只作比较，不判“合格”。"]
    lines += ["", "源稿 ICC 与目标 ICC 的 SHA-256 已写入 `color-audit.json`。"]
    with open(os.path.join(job_dir, "color-mapping.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    print(json.dumps({"ok": True, "colours": len(rows),
                      "maxDeltaE00": payload["summary"]["maxDeltaE00"],
                      "over5": payload["summary"]["over5"],
                      "tolerance": payload["tolerance"]["status"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
