#!/usr/bin/env python3
"""Stable hash of the acceptance parameters a Gate 3 result depends on."""

import hashlib
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))


def _sha(path):
    if not path or not os.path.exists(path):
        return None
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def payload(job, spec):
    brand_path = os.path.join(ROOT, "color", "brand-map.json")
    try:
        brand = json.load(open(brand_path, encoding="utf-8"))
    except Exception:
        brand = {}
    marks_path = os.path.join(ROOT, "config", "marks.json")
    target = (spec.get("print") or {}).get("iccProfile")
    payload = {
        "tacLimitPercent": job.get("tacLimitPercent", (spec.get("print") or {}).get("tacLimitPercent")),
        "pdfxStandard": job.get("pdfxStandard", (spec.get("print") or {}).get("pdfxStandard")),
        "bleedMm": job.get("bleedMm", (spec.get("print") or {}).get("bleedMm")),
        "trim": job.get("trim"),
        "pdfExport": job.get("pdfExport"),
        "pdfFileName": (job.get("output") or {}).get("pdf"),
        "targetICC": target,
        "targetICCsha256": _sha(target),
        "brandToleranceDeltaE00": brand.get("toleranceDeltaE00"),
        "brandMapSha256": _sha(brand_path),
        "marksSha256": _sha(marks_path),
    }
    if job.get("fold"):
        payload["fold"] = job["fold"]
    return payload


def digest(job, spec):
    data = json.dumps(payload(job, spec), ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(data).hexdigest()
