#!/usr/bin/env python3
"""ICC colour helpers built on the LittleCMS binaries that Ghostscript uses.

All Lab / ΔE00 values produced here are ICC predictions. They are not
measurements of printed paper. Keep that distinction in every report.
"""

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pdfprobe  # noqa: E402

def _bin(env_key, name, fallback):
    return os.environ.get(env_key) or shutil.which(name) or fallback


TRANSICC = _bin("TRANSICC_BIN", "transicc", "/opt/homebrew/bin/transicc")
GS = _bin("GS_BIN", "gs", "/opt/homebrew/bin/gs")
SRGB = os.environ.get("SRGB_ICC") or "/System/Library/ColorSync/Profiles/sRGB Profile.icc"


def profile_info(path):
    data = open(path, "rb").read()
    info = pdfprobe.profile_info(data)
    if info is None:
        raise ValueError("not an ICC profile: %s" % path)
    info["path"] = path
    return info


def _cgats(space, values):
    names = {"rgb": ["RGB_R", "RGB_G", "RGB_B"],
             "cmyk": ["CMYK_C", "CMYK_M", "CMYK_Y", "CMYK_K"],
             "lab": ["LAB_L", "LAB_A", "LAB_B"]}[space]
    lines = ["CGATS.17",
             "NUMBER_OF_FIELDS %d" % (len(names) + 1),
             "BEGIN_DATA_FORMAT",
             "SAMPLE_ID " + " ".join(names),
             "END_DATA_FORMAT",
             "NUMBER_OF_SETS %d" % len(values),
             "BEGIN_DATA"]
    for i, v in enumerate(values, 1):
        lines.append("%d %s" % (i, " ".join("%.6f" % x for x in v)))
    lines.append("END_DATA")
    return "\n".join(lines) + "\n"


def _transicc(src, dst, space, values, intent=1, bpc=False):
    if not values:
        return []
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "in.txt")
        with open(path, "w", encoding="ascii") as fh:
            fh.write(_cgats(space, values))
        cmd = [TRANSICC, "-i" + src, "-o" + dst, "-t%d" % intent, "-n"]
        if bpc:
            cmd.append("-b")
        cmd.append(path)
        out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise RuntimeError("transicc failed: %s\n%s" % (" ".join(cmd), out.stderr[-800:]))
    rows = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("LittleCMS") or line.startswith("Copyright"):
            continue
        parts = line.split()
        try:
            rows.append(tuple(float(x) for x in parts))
        except ValueError:
            continue
    if len(rows) != len(values):
        raise RuntimeError("transicc returned %d rows for %d colours" % (len(rows), len(values)))
    return rows


def rgb_to_lab(profile, colours, intent=1):
    """colours: [(r,g,b)] 0..255 -> [(L,a,b)] D50."""
    return _transicc(profile, "*Lab", "rgb", colours, intent=intent, bpc=False)


def rgb_to_cmyk(profile, colours, intent=1, bpc=True, dst_profile=None):
    """Predict sRGB -> CMYK under the conversion's target profile.

    dst_profile must be the ICC the conversion actually targeted
    (prepare.conversion.targetICC). There is no library default: a hard-coded
    target silently predicts against the wrong profile after the delivery ICC
    changes.
    """
    if not dst_profile:
        raise ValueError("rgb_to_cmyk needs dst_profile from the recorded conversion")
    return _transicc(profile, dst_profile, "rgb", colours,
                     intent=intent, bpc=bpc)


def cmyk_to_lab(colours, intent=1, src_profile=None):
    """Interpret CMYK numbers in src_profile (the recorded conversion target)."""
    if not src_profile:
        raise ValueError("cmyk_to_lab needs src_profile from the recorded conversion")
    return _transicc(src_profile, "*Lab", "cmyk", colours,
                     intent=intent, bpc=False)


def delta_e00(lab1, lab2):
    """CIEDE2000, kL=kC=kH=1, D50 reference values."""
    L1, a1, b1 = lab1
    L2, a2, b2 = lab2
    C1 = math.hypot(a1, b1)
    C2 = math.hypot(a2, b2)
    Cbar = (C1 + C2) / 2.0
    G = 0.5 * (1 - math.sqrt(Cbar ** 7 / (Cbar ** 7 + 25 ** 7))) if Cbar else 0.5
    a1p = (1 + G) * a1
    a2p = (1 + G) * a2
    C1p = math.hypot(a1p, b1)
    C2p = math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360 if (a1p or b1) else 0.0
    h2p = math.degrees(math.atan2(b2, a2p)) % 360 if (a2p or b2) else 0.0
    dLp = L2 - L1
    dCp = C2p - C1p
    if C1p * C2p == 0:
        dhp = 0.0
    elif abs(h2p - h1p) <= 180:
        dhp = h2p - h1p
    elif h2p - h1p > 180:
        dhp = h2p - h1p - 360
    else:
        dhp = h2p - h1p + 360
    dHp = 2 * math.sqrt(C1p * C2p) * math.sin(math.radians(dhp / 2.0))
    Lbar = (L1 + L2) / 2.0
    Cbarp = (C1p + C2p) / 2.0
    if C1p * C2p == 0:
        hbarp = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        hbarp = (h1p + h2p) / 2.0
    elif h1p + h2p < 360:
        hbarp = (h1p + h2p + 360) / 2.0
    else:
        hbarp = (h1p + h2p - 360) / 2.0
    T = (1 - 0.17 * math.cos(math.radians(hbarp - 30))
         + 0.24 * math.cos(math.radians(2 * hbarp))
         + 0.32 * math.cos(math.radians(3 * hbarp + 6))
         - 0.20 * math.cos(math.radians(4 * hbarp - 63)))
    dtheta = 30 * math.exp(-(((hbarp - 275) / 25.0) ** 2))
    RC = 2 * math.sqrt(Cbarp ** 7 / (Cbarp ** 7 + 25 ** 7)) if Cbarp else 0.0
    SL = 1 + (0.015 * (Lbar - 50) ** 2) / math.sqrt(20 + (Lbar - 50) ** 2)
    SC = 1 + 0.045 * Cbarp
    SH = 1 + 0.015 * Cbarp * T
    RT = -math.sin(math.radians(2 * dtheta)) * RC
    return math.sqrt((dLp / SL) ** 2 + (dCp / SC) ** 2 + (dHp / SH) ** 2
                     + RT * (dCp / SC) * (dHp / SH))


def delta_e_selftest():
    # Sharma, Wu & Dalal (2005), pair 1 and pair 2.
    pairs = [
        ((50.0, 2.6772, -79.7751), (50.0, 0.0, -82.7485), 2.0425),
        ((50.0, 3.1571, -77.2803), (50.0, 0.0, -82.7485), 2.8615),
        ((50.0, 2.8361, -74.0200), (50.0, 0.0, -82.7485), 3.4412),
    ]
    out = []
    for a, b, expected in pairs:
        got = delta_e00(a, b)
        out.append({"lab1": a, "lab2": b, "expected": expected,
                    "got": round(got, 4), "pass": abs(got - expected) <= 0.0002})
    return out


def _patch_pdf(colours, path):
    ps = ["<< /PageSize [72 72] >> setpagedevice"]
    for r, g, b in colours:
        ps.append("%.6f %.6f %.6f setrgbcolor 0 0 72 72 rectfill showpage"
                  % (r / 255.0, g / 255.0, b / 255.0))
    ps_path = path + ".ps"
    with open(ps_path, "w", encoding="ascii") as fh:
        fh.write("\n".join(ps) + "\n")
    subprocess.run([GS, "-q", "-dBATCH", "-dNOPAUSE", "-dSAFER",
                    "-sDEVICE=pdfwrite", "-o", path, ps_path],
                   capture_output=True, check=True)
    os.unlink(ps_path)


def actual_cmyk(gs_args, colour, cache_path=None):
    """Run the production Ghostscript colour path for one RGB colour.

    gs_args is prepare.json's conversion.gsArgs (without -o and input).
    Returns a dict with the CMYK tuple and the command actually used.
    """
    key = hashlib.sha256(("|".join(gs_args) + "|%d,%d,%d" % colour).encode()).hexdigest()
    if cache_path and os.path.exists(cache_path):
        try:
            cache = json.load(open(cache_path, encoding="utf-8"))
            if key in cache:
                return cache[key]
        except Exception:
            cache = {}
    else:
        cache = {}
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "patch.pdf")
        dst = os.path.join(tmp, "cmyk.pdf")
        _patch_pdf([colour], src)
        cmd = [GS] + list(gs_args) + ["-o", dst, src]
        run = subprocess.run(cmd, capture_output=True, check=False)
        if run.returncode != 0:
            err = run.stderr.decode("utf-8", "replace")[-800:]
            raise RuntimeError("gs patch probe failed: %s\n%s" % (" ".join(cmd), err))
        info = pdfprobe.inspect(dst)
    cmyk = [c for c in info["colours"] if c["space"] == "cmyk" and c["count"] > 0]
    if not cmyk:
        raise RuntimeError("no CMYK operator in patch output for %r" % (colour,))
    # A full-page patch should have one dominant CMYK operator.
    cmyk.sort(key=lambda x: -x["count"])
    result = {"rgb": list(colour), "cmyk": cmyk[0]["cmyk"],
              "operators": sum(c["count"] for c in cmyk),
              "gsArgs": gs_args, "command": " ".join(cmd)}
    if cache_path:
        cache[key] = result
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, ensure_ascii=False, indent=2)
    return result


def round_lab(lab):
    return [round(x, 4) for x in lab]


def describe(path):
    info = profile_info(path)
    return {k: info[k] for k in ("description", "space", "pcs", "id", "sha256", "bytes")}
