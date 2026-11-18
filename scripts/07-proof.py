#!/usr/bin/env python3
"""07-proof.py <job-id> [--dpi 150]

Render the RGB source, the delivery-ICC CMYK intermediate and (when present)
the Illustrator production file. The CMYK→sRGB conversions are ICC
simulations made with the same LittleCMS build Ghostscript uses.

This is a software proof, not an Illustrator Proof Setup capture and not
a contract proof. The report says so explicitly.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "lib"))
import color  # noqa: E402
import raster  # noqa: E402
import source  # noqa: E402


def load(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def run(cmd):
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError("%s\n%s" % (" ".join(cmd), r.stderr.decode("utf-8", "replace")[-800:]))


def main():
    args = sys.argv[1:]
    if not args:
        print("usage: 07-proof.py <job-id> [--dpi 150]", file=sys.stderr)
        return 64
    job_id = args[0]
    dpi = 150
    if "--dpi" in args:
        dpi = int(args[args.index("--dpi") + 1])
    job_dir = os.path.join(ROOT, "jobs", job_id)
    job = load(os.path.join(job_dir, "job.json"))
    spec = load(os.path.join(ROOT, "config", "print-spec.json"))
    prepare = load(os.path.join(job_dir, "prepare.json"))
    if not job or not prepare:
        print("missing job.json or prepare.json", file=sys.stderr)
        return 66

    src = os.path.join(job_dir, job["source"]["pdf"])
    cmyk = os.path.join(job_dir, prepare["output"])
    ai = os.path.join(job_dir, job["output"]["ai"])
    outdir = os.path.join(job_dir, "work", "proof")
    os.makedirs(outdir, exist_ok=True)

    gs = prepare["conversion"]["gsBinary"]
    fogra = prepare["conversion"]["targetICC"]
    srgb = color.SRGB
    bleed = float(job.get("bleedMm", 3) or 0)
    page_scale = source.page_scale(job, spec)

    paths = {
        "sourceTif": os.path.join(outdir, "source-rgb.tif"),
        "cmykTif": os.path.join(outdir, "cmyk-raw.tif"),
        "simTif": os.path.join(outdir, "cmyk-sim.tif"),
        "paperTif": os.path.join(outdir, "cmyk-paper.tif"),
        "aiTif": os.path.join(outdir, "ai-cmyk.tif"),
        "aiSimTif": os.path.join(outdir, "ai-sim.tif"),
        "sourcePng": os.path.join(outdir, "source-rgb.png"),
        "simPng": os.path.join(outdir, "cmyk-sim.png"),
        "paperPng": os.path.join(outdir, "cmyk-paper.png"),
        "sidePng": os.path.join(outdir, "source-vs-cmyk.png"),
        "threePng": os.path.join(outdir, "source-cmyk-paper.png"),
        "diffPng": os.path.join(outdir, "difference-map.png"),
        "aiPng": os.path.join(outdir, "ai-sim.png"),
        "aiSidePng": os.path.join(outdir, "cmyk-vs-ai.png"),
    }

    result = {
        "jobId": job_id,
        "ok": False,
        "render": {"dpi": dpi, "gs": prepare["conversion"]["gsVersion"],
                   "tificc": "LittleCMS (tificc)", "srgb": srgb,
                   "fogra39": fogra, "bleedMm": bleed},
        "hashes": {
            "source": prepare.get("inputSha256"),
            "cmyk": prepare.get("outputSha256"),
            "ai": None,
        },
        "proof": {
            "illustratorSoftproof": "pending",
            "lcmsSimulation": "complete",
            "note": "ICC soft proof only. Illustrator Proof Setup and contract proof are separate checks.",
        },
        "files": {},
        "warnings": [],
    }

    # Source RGB at requested dpi.
    run([gs, "-q", "-dBATCH", "-dNOPAUSE", "-dSAFER", "-sDEVICE=tiff24nc",
         "-r%d" % dpi, "-o", paths["sourceTif"], src])
    src_tif = raster.read_tiff(paths["sourceTif"])
    source_rgb = src_tif["data"][..., :3]

    # CMYK intermediate at the same page geometry.
    run([gs, "-q", "-dBATCH", "-dNOPAUSE", "-dSAFER", "-sDEVICE=tiff32nc",
         "-r%d" % dpi, "-o", paths["cmykTif"], cmyk])

    # Relative + BPC (the production intent) and absolute (paper white).
    run(["tificc", "-n", "-i" + fogra, "-o" + srgb, "-t1", "-b", "-w8",
         paths["cmykTif"], paths["simTif"]])
    run(["tificc", "-n", "-i" + fogra, "-o" + srgb, "-t3", "-w8",
         paths["cmykTif"], paths["paperTif"]])
    sim_rgb = raster.read_tiff(paths["simTif"])["data"][..., :3]
    paper_rgb = raster.read_tiff(paths["paperTif"])["data"][..., :3]

    raster.write_png(paths["sourcePng"], source_rgb)
    raster.write_png(paths["simPng"], sim_rgb)
    raster.write_png(paths["paperPng"], paper_rgb)
    side = raster.side_by_side([source_rgb, sim_rgb])
    raster.write_png(paths["sidePng"], side)
    scale = 700.0 / max(1, source_rgb.shape[1])
    small = [raster.resize_nearest(im, int(im.shape[1] * scale), int(im.shape[0] * scale))
             for im in (source_rgb, sim_rgb, paper_rgb)]
    raster.write_png(paths["threePng"], raster.side_by_side(small))
    raster.write_difference_map(paths["diffPng"], source_rgb, sim_rgb, threshold=5.0)

    rgb_ai_path = os.path.join(job_dir, "work", "rgb-ai.tif")
    if os.path.exists(rgb_ai_path):
        try:
            rgb_ai = raster.read_tiff(rgb_ai_path)["data"][..., :3]
            result["sourceVsRgbAi"] = raster.compare_aligned(
                source_rgb, rgb_ai, max_shift=1, flat=True, flat_radius=5)
            result["sourceVsRgbAi"]["status"] = (
                "pass" if result["sourceVsRgbAi"]["median"] <= 1.0
                and result["sourceVsRgbAi"]["over5Percent"] <= 0.05 else "attention")
        except Exception as exc:  # noqa: BLE001
            result["sourceVsRgbAi"] = {"status": "pending", "error": str(exc)}
    else:
        result["sourceVsRgbAi"] = {"status": "pending", "error": "rgb-ai.tif missing"}

    result["sourceVsCmyk"] = raster.compare(source_rgb, sim_rgb, flat=True)
    result["sourceVsPaper"] = raster.compare(source_rgb, paper_rgb, flat=True)
    result["sourceVsCmyk"]["status"] = (
        "review" if result["sourceVsCmyk"]["median"] <= 2.0
        and result["sourceVsCmyk"]["over10Percent"] <= 1.0 else "attention")

    # AI is optional until 02 has run.
    if os.path.exists(ai):
        result["hashes"]["ai"] = hashlib.sha256(open(ai, "rb").read()).hexdigest()
        try:
            ai_dpi = int(round(dpi / page_scale))
            run([gs, "-q", "-dBATCH", "-dNOPAUSE", "-dSAFER", "-sDEVICE=tiff32nc",
                 "-r%d" % ai_dpi, "-o", paths["aiTif"], ai])
            run(["tificc", "-n", "-i" + fogra, "-o" + srgb, "-t1", "-b", "-w8",
                 paths["aiTif"], paths["aiSimTif"]])
            ai_rgb = raster.read_tiff(paths["aiSimTif"])["data"][..., :3]
            raster.write_png(paths["aiPng"], ai_rgb)
            bleed_px = int(round((bleed / page_scale) / 25.4 * dpi))
            h, w = sim_rgb.shape[:2]
            trimmed = raster.crop(sim_rgb, bleed_px, bleed_px, w - bleed_px, h - bleed_px)
            hh = min(trimmed.shape[0], ai_rgb.shape[0])
            ww = min(trimmed.shape[1], ai_rgb.shape[1])
            trimmed = raster.resize_nearest(trimmed, ww, hh)
            ai_rgb = raster.resize_nearest(ai_rgb, ww, hh)
            raster.write_png(paths["aiSidePng"], raster.side_by_side([trimmed, ai_rgb]))
            result["cmykVsAi"] = raster.compare_aligned(trimmed, ai_rgb, max_shift=2, flat=True, flat_radius=5)
            result["cmykVsAi"]["expected"] = {
                "aiDpi": ai_dpi, "bleedCropPx": bleed_px, "pageScale": page_scale,
                "aiSize": [ai_rgb.shape[1], ai_rgb.shape[0]],
                "trimmedCmykSize": [trimmed.shape[1], trimmed.shape[0]],
            }
            result["cmykVsAi"]["note"] = (
                "Solid-colour comparison; a 1px scale/resampling residual on thin "
                "frame lines is excluded by the flat-radius mask. Exact CMYK "
                "values are checked separately by 03-qa.")
            result["cmykVsAi"]["status"] = (
                "pass" if result["cmykVsAi"]["median"] <= 0.5
                and result["cmykVsAi"]["over5Percent"] <= 0.05 else "attention")
        except Exception as exc:  # noqa: BLE001
            result["warnings"].append("AI render/simulation failed: %s" % exc)
            result["cmykVsAi"] = {"status": "pending", "error": str(exc)}
    else:
        result["cmykVsAi"] = {"status": "pending", "error": "AI not generated yet"}

    # Keep the reviewable PNGs and JSON; regenerate TIFFs on demand.
    for key in ("sourceTif", "cmykTif", "simTif", "paperTif", "aiTif", "aiSimTif"):
        try:
            if os.path.exists(paths[key]):
                os.unlink(paths[key])
        except OSError:
            pass
    result["files"] = {k: os.path.relpath(v, job_dir) for k, v in paths.items() if os.path.exists(v)}
    result["ok"] = True
    result["generatedAt"] = __import__("datetime").datetime.now().isoformat(timespec="seconds")
    with open(os.path.join(job_dir, "proof.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)

    print(json.dumps({"ok": True, "sourceVsCmyk": result["sourceVsCmyk"],
                      "cmykVsAi": result.get("cmykVsAi", {}).get("status")},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
