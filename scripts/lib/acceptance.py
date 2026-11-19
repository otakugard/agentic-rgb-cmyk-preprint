#!/usr/bin/env python3
"""Stable hash of the acceptance parameters a Gate 3 result depends on."""

import glob
import hashlib
import json
import os
import shutil
import subprocess

import contract as colorcontract

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
GATE_POLICY_VERSION = 3


def _sha(path):
    if not path or not os.path.exists(path):
        return None
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def _control_icc():
    candidates = sorted(glob.glob(
        "/opt/homebrew/Cellar/ghostscript/*/share/ghostscript/iccprofiles/default_cmyk.icc"))
    candidates.append(
        "/Library/Application Support/Adobe/Color/Profiles/Recommended/USWebCoatedSWOP.icc")
    return next((p for p in candidates if os.path.exists(p)), None)


def _toolchain():
    def version(cmd):
        try:
            run = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            text = (run.stdout or "") + (run.stderr or "")
            return text.strip().splitlines()[0] if text.strip() else None
        except Exception:
            return None
    return {
        "gs": version(["gs", "--version"]),
        "mupdf": version(["mutool", "-v"]),
        "tificc": version(["tificc", "-v"]),
        "qpdf": version(["qpdf", "--version"]),
    }


def payload(job, spec):
    brand_path = os.path.join(ROOT, "color", "brand-map.json")
    try:
        brand = json.load(open(brand_path, encoding="utf-8"))
    except Exception:
        brand = {}
    try:
        color_contract = colorcontract.load()
        contract_sha = colorcontract.contract_sha(color_contract)
        target = colorcontract.profile(color_contract, "target")[0]
    except Exception:
        color_contract = {}
        contract_sha = None
        target = None
    job_id = str(job.get("jobId") or "")
    job_dir = os.path.join(ROOT, "jobs", job_id)
    trim = job.get("trim") or {}
    trim_mm = trim.get("w"), trim.get("h")
    if trim.get("preset"):
        preset = (spec.get("trimPresetsMm") or {}).get(trim["preset"])
        if preset:
            trim_mm = tuple(preset)
    control = _control_icc()
    return {
        "gatePolicyVersion": GATE_POLICY_VERSION,
        "tacLimitPercent": job.get("tacLimitPercent", (spec.get("print") or {}).get("tacLimitPercent")),
        "pdfxStandard": job.get("pdfxStandard", (spec.get("print") or {}).get("pdfxStandard")),
        "bleedMm": job.get("bleedMm", (spec.get("print") or {}).get("bleedMm")),
        "trim": job.get("trim"),
        "trimMm": list(trim_mm),
        "fold": job.get("fold"),
        "figma": job.get("figma"),
        "pdfExport": job.get("pdfExport"),
        "pdfFileName": (job.get("output") or {}).get("pdf"),
        "targetICC": target,
        "targetICCsha256": _sha(target),
        "colorContract": color_contract.get("contractId"),
        "colorContractVersion": color_contract.get("version"),
        "colorContractSha256": contract_sha,
        "brandToleranceDeltaE00": brand.get("toleranceDeltaE00"),
        "brandMapSha256": _sha(brand_path),
        "marksSha256": _sha(os.path.join(ROOT, "config", "marks.json")),
        "conversionSha256": _sha(os.path.join(job_dir, "work", "conversion.json")),
        "prepareSha256": _sha(os.path.join(job_dir, "prepare.json")),
        "cmykFinalSha256": _sha(os.path.join(job_dir, "work", "cmyk-final.pdf")),
        "controlICC": control,
        "controlICCsha256": _sha(control),
        "toolchain": _toolchain(),
    }
    if job.get("fold"):
        payload["fold"] = job["fold"]
    return payload


def digest(job, spec):
    data = json.dumps(payload(job, spec), ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(data).hexdigest()
