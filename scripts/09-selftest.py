#!/usr/bin/env python3
"""09-selftest.py

Negative fixtures for the preflight checks. These are test PDFs in a
temporary directory, not delivery files and not production jobs.

Asserts that 05-preflight.py catches:
  - white pixels inside the final 3 mm bleed band
  - a visible 325.9% TAC patch when the printer limit is 300%
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

spec = importlib.util.spec_from_file_location(
    "preflight", os.path.join(HERE, "05-preflight.py"))
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        job_dir = os.path.join(tmp, "job")
        os.makedirs(os.path.join(job_dir, "work"))
        job = {
            "jobId": "selftest-preflight",
            "trim": {"preset": "A5"},
            "bleedMm": 3,
            "tacLimitPercent": 300,
            "source": {"pdf": "source/x.pdf"},
            "output": {"ai": "x.ai", "pdf": "x.pdf"},
        }
        prep = {
            "output": "work/cmyk.pdf",
            "pageMm": [205.33, 288.0],
            "outputSha256": "selftest",
        }
        json.dump(job, open(os.path.join(job_dir, "job.json"), "w"))
        json.dump(prep, open(os.path.join(job_dir, "prepare.json"), "w"))

        ps = os.path.join(tmp, "fixture.ps")
        pdf = os.path.join(job_dir, "work", "cmyk.pdf")
        with open(ps, "w", encoding="ascii") as fh:
            fh.write("""<< /PageSize [582.047 816.378] >> setpagedevice
0.05 0.02 0.01 0 setcmykcolor 0 0 582.047 816.378 rectfill
0 0 0 0 setcmykcolor 0 0 10 816.378 rectfill
0.89 0.784 0.616 0.969 setcmykcolor 50 50 20 20 rectfill
showpage
""")
        subprocess.run([shutil.which("gs") or "/opt/homebrew/bin/gs", "-q", "-dBATCH", "-dNOPAUSE",
                        "-dSAFER", "-sDEVICE=pdfwrite", "-o", pdf, ps], check=True)
        real_spec = json.load(open(os.path.join(ROOT, "config", "print-spec.json")))
        res = preflight.analyse(job_dir, job, real_spec)

        checks = {
            "bleedWhiteDetected": {
                "pass": res["bleed"]["whitePixels"] > 0,
                "value": res["bleed"]["whitePixels"],
                "expected": "> 0",
            },
            "tacOverLimitDetected": {
                "pass": res["tac"]["status"] == "fail",
                "value": {"status": res["tac"]["status"],
                          "visibleMax": res["tac"].get("visibleMaxTacPercent"),
                          "vector": res["vectorMaxTac"]},
                "expected": "fail at limit 300%",
            },
            "bleedBandIsFull3mm": {
                "pass": res["bleed"]["bandMmActual"]["x"] >= 3.0,
                "value": res["bleed"]["bandMmActual"],
                "expected": ">= 3.0 mm final",
            },
        }
        out = {
            "ok": all(c["pass"] for c in checks.values()),
            "fixture": "temporary; not a delivery PDF",
            "checks": checks,
        }
        os.makedirs(os.path.join(ROOT, "tests"), exist_ok=True)
        json.dump(out, open(os.path.join(ROOT, "tests", "preflight-selftest.json"), "w"),
                  ensure_ascii=False, indent=2)
        print(json.dumps(out, ensure_ascii=False))
        return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
