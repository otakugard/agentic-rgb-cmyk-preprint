#!/usr/bin/env python3
"""05-preflight.py <job-id>

Inspect the actual work/cmyk.pdf:
  - structure from the original file (object streams included), not a GS rewrite
  - full bleed band, converted back to final trim coordinates
  - TAC at 150 and 300 dpi, plus vector-exact max TAC
  - neutral dark / four-colour grey / pure K statistics

`ok` only means the analysis ran. Print acceptance still depends on the
printer's TAC limit and contract proof.
"""

import json
import math
import os
import subprocess
import sys
import tempfile
import shutil

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "lib"))
import pdfprobe  # noqa: E402
import contract as colorcontract  # noqa: E402
import raster  # noqa: E402

GS = shutil.which("gs") or "/opt/homebrew/bin/gs"


def load(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def trim_mm(job, spec):
    if job.get("trim", {}).get("preset"):
        w, h = spec["trimPresetsMm"][job["trim"]["preset"]]
        return float(w), float(h)
    return float(job["trim"]["w"]), float(job["trim"]["h"])


def render_cmyk(pdf, dpi, out):
    # TAC is measured on stored DeviceCMYK operands, not on a colour-managed
    # reading. FastColor keeps the file's ink numbers; the file has no
    # /DefaultCMYK, so DeviceCMYK is device CMYK under the OutputIntent.
    subprocess.run([GS, "-q", "-dBATCH", "-dNOPAUSE", "-dSAFER",
                    "-dUseFastColor=true",
                    "-sDEVICE=tiff32nc", "-r%d" % dpi, "-o", out, pdf],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return raster.read_tiff(out)["data"].astype(np.int16)


def tac_stats(arr):
    tac = arr.sum(axis=2) * 100.0 / 255.0
    values = tac.ravel()
    return {
        "maxTacPercent": round(float(values.max()), 2),
        "maxTacPixel": [int(x) for x in np.unravel_index(int(values.argmax()), tac.shape)][::-1],
        "shareOver300Percent": round(float((values > 300).mean() * 100), 3),
        "shareOver330Percent": round(float((values > 330).mean() * 100), 3),
        "p999TacPercent": round(float(np.percentile(values, 99.9)), 2),
        "pixels": int(values.size),
    }


def side_stats(mask):
    return {
        "bandPx": [int(mask.shape[1]), int(mask.shape[0])],
        "whitePixels": int(np.count_nonzero(mask)),
    }


def analyse(job_dir, job, spec):
    cmyk = os.path.join(job_dir, "work", "cmyk.pdf")
    if not os.path.exists(cmyk):
        raise RuntimeError("missing work/cmyk.pdf")
    info = pdfprobe.inspect(cmyk)
    prepare = load(os.path.join(job_dir, "prepare.json"), {}) or {}
    page = prepare.get("pageMm") or [0, 0]
    tw, th = trim_mm(job, spec)
    b = float(job.get("bleedMm", spec["print"].get("bleedMm", 0)) or 0)
    if not page or not page[0] or not page[1]:
        raise RuntimeError("prepare.json has no pageMm; cannot convert bleed to final coordinates")
    scale_x = page[0] / (tw + 2 * b)
    scale_y = page[1] / (th + 2 * b)

    result = {
        "ok": True,
        "jobId": job.get("jobId"),
        "sourcePdfSha256": prepare.get("outputSha256"),
        "pixels": {},
        "bleed": {},
        "tac": {},
        "neutral": {},
        "structure": info["structure"],
        "vectorMaxTac": info.get("vectorMaxTac"),
        "colours": {
            "rgb": sum(1 for c in info["colours"] if c["space"] == "rgb"),
            "cmyk": sum(1 for c in info["colours"] if c["space"] == "cmyk"),
            "gray": sum(1 for c in info["colours"] if c["space"] == "gray"),
            "cmykOperators": sum(c["count"] for c in info["colours"] if c["space"] == "cmyk"),
            "rgbOperators": sum(c["count"] for c in info["colours"] if c["space"] == "rgb"),
        },
        "scales": {"x": round(scale_x, 5), "y": round(scale_y, 5)},
        "notes": [
            "Structure is read from the original cmyk.pdf, including object streams.",
            "TAC max is a raster sample; vectorMaxTac is exact for flat CMYK fills.",
        ],
    }

    with tempfile.TemporaryDirectory() as tmp:
        for dpi in (150, 300):
            tif = os.path.join(tmp, "page-%d.tif" % dpi)
            arr = render_cmyk(cmyk, dpi, tif)
            h, w = arr.shape[:2]
            result["pixels"][str(dpi)] = [w, h]
            stats = tac_stats(arr)
            if dpi == 150:
                result["tac"].update(stats)
                result["tac"]["dpi"] = 150
            else:
                result["tac"]["dpi300"] = stats

            # Full bleed band in final trim coordinates.
            # cmyk.pdf is scale_x times the final size. A final 3 mm bleed
            # occupies 3*scale_x mm in this file; do not divide by scale.
            band_x = int(math.ceil(b * scale_x / 25.4 * dpi))
            band_y = int(math.ceil(b * scale_y / 25.4 * dpi))
            white = np.all(arr == 0, axis=2)
            sides = {
                "left": side_stats(white[:, :band_x]),
                "right": side_stats(white[:, w - band_x:]),
                "top": side_stats(white[:band_y, :]),
                "bottom": side_stats(white[h - band_y:, :]),
            }
            total_band = band_x * h * 2 + band_y * w * 2 - 4 * band_x * band_y
            white_total = int(sum(v["whitePixels"] for v in sides.values()))
            result["bleed"] = {
                "bleedMm": b,
                "bandPx": {"x": band_x, "y": band_y},
                "bandMmActual": {
                    "x": round(band_x / dpi * 25.4 / scale_x, 3),
                    "y": round(band_y / dpi * 25.4 / scale_y, 3),
                },
                "sides": sides,
                "whitePixels": white_total,
                "whitePercent": round(white_total / total_band * 100, 4) if total_band else None,
                "dpi": dpi,
            }
            if dpi == 300:
                # Is the highest-TAC vector colour actually visible? A clipped
                # or fully occluded object can sit in the content stream but
                # never reach paper.
                vec = result.get("vectorMaxTac")
                if vec and vec.get("cmyk"):
                    # vec["cmyk"] is percent (0..100); arr is 0..255.
                    target = np.array(vec["cmyk"], dtype=np.float32) * 255.0 / 100.0
                    near = np.abs(arr.astype(np.float32) - target).max(axis=2) <= 10
                    vec["visiblePixels300"] = int(np.count_nonzero(near))
                    vec["visibleAt300"] = vec["visiblePixels300"] > 0
                # Neutral / black statistics at 300 dpi: small text and thin
                # marks are more likely to have solid cores at this resolution.
                mx = arr.max(axis=2)
                mn = arr.min(axis=2)
                k = arr[..., 3]
                neutral = (mx - mn <= 60) & (k >= 80)
                pure_k = (arr[..., 0] == 0) & (arr[..., 1] == 0) & (arr[..., 2] == 0) & (k > 0)
                four = neutral & (arr[..., 0] > 0) & (arr[..., 1] > 0) & (arr[..., 2] > 0)
                tac = arr.sum(axis=2) * 100.0 / 255.0
                neutral_tac = tac[neutral]
                result["neutral"] = {
                    "neutralDarkPixels": int(np.count_nonzero(neutral)),
                    "neutralDarkPercent": round(float(neutral.mean() * 100), 3),
                    "neutralDarkMaxTac": round(float(neutral_tac.max()), 2) if neutral_tac.size else None,
                    "pureKPercent": round(float(pure_k.mean() * 100), 3),
                    "fourColourGreyPercent": round(float(four.mean() * 100), 3),
                    "thresholds": "max-min <= 23.5% and K >= 31% at 300 dpi",
                    "note": "Raster heuristic. Vector exact values are in vectorMaxTac and colours.",
                }

    # Summary status. Never claims print acceptance.
    tac_limit = job.get("tacLimitPercent")
    if tac_limit is None:
        tac_limit = spec["print"].get("tacLimitPercent")
    vec_info = result.get("vectorMaxTac") or {}
    visible_vector = vec_info.get("tac", 0) if vec_info.get("visibleAt300") else 0
    visible_max = max(result["tac"]["maxTacPercent"],
                      (result["tac"].get("dpi300") or {}).get("maxTacPercent", 0),
                      visible_vector)
    result["tac"]["visibleMaxTacPercent"] = round(visible_max, 2)
    result["tac"]["visibleVectorTac"] = visible_vector
    if tac_limit is None:
        tac_status = "pending"
        tac_note = "Printer TAC limit not confirmed; value is reported, not judged."
    elif visible_max > float(tac_limit):
        tac_status = "fail"
        tac_note = "Max visible TAC %.2f%% exceeds confirmed limit %s%%." % (visible_max, tac_limit)
    else:
        tac_status = "pass"
        tac_note = "Max visible TAC %.2f%% within confirmed limit %s%%. Requires print-shop confirmation." % (visible_max, tac_limit)
    result["tac"]["status"] = tac_status
    result["tac"]["limitPercent"] = tac_limit
    result["tac"]["note"] = tac_note

    result["bleed"]["status"] = "pass" if result["bleed"]["whitePixels"] == 0 else "attention"
    result["bleed"]["note"] = (
        "Zero pure-white pixels in the full band. White may still be intentional; "
        "check the design and the final PDF BleedBox.")
    result["structure"]["status"] = (
        "attention" if (result["structure"].get("deviceRGB", 0) > 0
                        or result["structure"].get("separation", 0) > 0
                        or result["structure"].get("allSeparation")
                        or result["structure"].get("overprintTrue", 0) > 0) else "pass")
    bleed_bad = result["bleed"]["whitePixels"] > 0
    result["status"] = "fail" if tac_status == "fail" else (
        "pending" if tac_status == "pending" else
        "attention" if (bleed_bad or result["structure"]["status"] == "attention") else "pass")
    return result


def main():
    if len(sys.argv) != 2:
        print("usage: 05-preflight.py <job-id>", file=sys.stderr)
        return 64
    job_id = sys.argv[1]
    job_dir = os.path.join(ROOT, "jobs", job_id)
    if not os.path.isdir(job_dir):
        print("job dir not found: %s" % job_dir, file=sys.stderr)
        return 66
    spec = load(os.path.join(ROOT, "config", "print-spec.json"))
    global GS
    _gs = ((spec or {}).get("ghostscript") or {}).get("binary") or "gs"
    GS = shutil.which(_gs) or _gs
    try:
        spec = colorcontract.apply_to_spec(spec)
    except Exception as exc:  # noqa: BLE001
        print("colour contract error: %s" % exc, file=sys.stderr)
        return 78
    job = load(os.path.join(job_dir, "job.json"))
    try:
        result = analyse(job_dir, job, spec)
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "error": str(exc)}
    with open(os.path.join(job_dir, "preflight.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    print(json.dumps({k: result.get(k) for k in
                      ("ok", "status", "bleed", "tac", "structure", "neutral", "vectorMaxTac")},
                     ensure_ascii=False))
    return 0 if result.get("ok") else 70


if __name__ == "__main__":
    sys.exit(main())
