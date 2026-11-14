#!/usr/bin/env python3
"""Small raster helpers: uncompressed TIFF in, PNG out, ΔE00 statistics.

No Pillow. numpy is used for image maths. TIFF support is intentionally
limited to the uncompressed 8-bit strips produced by Ghostscript,
tificc and Illustrator's TIFF exporter.
"""

import struct
import zlib

import numpy as np


def read_tiff(path):
    data = open(path, "rb").read()
    bo = "<" if data[:2] == b"II" else ">"
    ifd = struct.unpack(bo + "I", data[4:8])[0]
    count = struct.unpack(bo + "H", data[ifd:ifd + 2])[0]
    tags = {}
    for i in range(count):
        off = ifd + 2 + 12 * i
        tag, typ, cnt = struct.unpack(bo + "HHI", data[off:off + 8])
        raw = data[off + 8:off + 12]
        if typ == 3 and cnt == 1:
            val = struct.unpack(bo + "H", raw[:2])[0]
        elif typ == 4 and cnt == 1:
            val = struct.unpack(bo + "I", raw)[0]
        elif typ == 3:
            ptr = struct.unpack(bo + "I", raw)[0]
            val = struct.unpack(bo + "%dH" % cnt, data[ptr:ptr + 2 * cnt])
        elif typ == 4:
            ptr = struct.unpack(bo + "I", raw)[0]
            val = struct.unpack(bo + "%dI" % cnt, data[ptr:ptr + 4 * cnt])
        else:
            val = raw
        tags[tag] = val

    width = tags[256]
    height = tags[257]
    bits = tags.get(258, (8,))
    if isinstance(bits, int):
        bits = (bits,)
    if any(b != 8 for b in bits):
        raise RuntimeError("only 8-bit TIFF is supported: bits=%s" % (bits,))
    spp = tags.get(277, 1)
    compression = tags.get(259, 1)
    photometric = tags.get(262, 2)
    if compression != 1:
        raise RuntimeError("TIFF is compressed (compression=%s); export uncompressed" % compression)
    if photometric not in (1, 2, 5):
        raise RuntimeError("unsupported TIFF photometric=%s" % photometric)
    offsets = tags.get(273)
    byte_counts = tags.get(279)
    if not isinstance(offsets, tuple):
        offsets = (offsets,)
    if not isinstance(byte_counts, tuple):
        byte_counts = (byte_counts,)
    buf = b"".join(data[o:o + n] for o, n in zip(offsets, byte_counts))
    expected = width * height * spp
    if len(buf) < expected:
        raise RuntimeError("TIFF data short: %d < %d" % (len(buf), expected))
    arr = np.frombuffer(buf[:expected], dtype=np.uint8)
    if spp == 1:
        arr = arr.reshape(height, width)
    else:
        arr = arr.reshape(height, width, spp)
    return {"width": width, "height": height, "spp": spp,
            "photometric": photometric, "data": arr}


def write_png(path, arr):
    """arr: HxWx3 or HxW uint8."""
    arr = np.ascontiguousarray(arr.astype(np.uint8))
    if arr.ndim == 2:
        arr = np.stack([arr, arr, arr], axis=-1)
    h, w, _ = arr.shape
    raw = b"".join(b"\x00" + arr[y].tobytes() for y in range(h))

    def chunk(kind, payload):
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 6))
    png += chunk(b"IEND", b"")
    open(path, "wb").write(png)


def cmyk_to_rgb_naive(arr):
    """Fallback only. For proof images use tificc, not this."""
    c = arr.astype(np.float32) / 255.0
    r = (1 - c[..., 0]) * (1 - c[..., 3])
    g = (1 - c[..., 1]) * (1 - c[..., 3])
    b = (1 - c[..., 2]) * (1 - c[..., 3])
    return np.stack([r, g, b], axis=-1)


def crop(arr, left, top, right, bottom):
    h, w = arr.shape[:2]
    left = max(0, min(w, left))
    top = max(0, min(h, top))
    right = max(left + 1, min(w, right))
    bottom = max(top + 1, min(h, bottom))
    return arr[top:bottom, left:right]


def resize_nearest(arr, width, height):
    h, w = arr.shape[:2]
    ys = (np.arange(height) * h / height).astype(np.int32).clip(0, h - 1)
    xs = (np.arange(width) * w / width).astype(np.int32).clip(0, w - 1)
    return arr[ys][:, xs]


def side_by_side(images, gap=8, bg=(255, 255, 255)):
    h = max(im.shape[0] for im in images)
    w = sum(im.shape[1] for im in images) + gap * (len(images) - 1)
    out = np.zeros((h, w, 3), dtype=np.uint8)
    out[:, :] = bg
    x = 0
    for im in images:
        out[:im.shape[0], x:x + im.shape[1]] = im
        x += im.shape[1] + gap
    return out


def srgb_to_lab(rgb):
    """Vectorised sRGB (0..255, D65) -> CIELAB. For rendered-image stats."""
    x = rgb.astype(np.float64) / 255.0
    x = np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)
    m = np.array([[0.4124564, 0.3575761, 0.1804375],
                  [0.2126729, 0.7151522, 0.0721750],
                  [0.0193339, 0.1191920, 0.9503041]])
    xyz = x @ m.T
    white = np.array([0.95047, 1.0, 1.08883])
    xyz = xyz / white
    f = np.where(xyz > 216 / 24389, np.cbrt(xyz), (24389 / 27 * xyz + 16) / 116)
    L = 116 * f[..., 1] - 16
    a = 500 * (f[..., 0] - f[..., 1])
    b = 200 * (f[..., 1] - f[..., 2])
    return np.stack([L, a, b], axis=-1)


def delta_e00_array(lab1, lab2):
    L1, a1, b1 = lab1[..., 0], lab1[..., 1], lab1[..., 2]
    L2, a2, b2 = lab2[..., 0], lab2[..., 1], lab2[..., 2]
    C1 = np.hypot(a1, b1)
    C2 = np.hypot(a2, b2)
    Cbar = (C1 + C2) / 2
    G = 0.5 * (1 - np.sqrt(Cbar ** 7 / (Cbar ** 7 + 25 ** 7 + 1e-30)))
    a1p = (1 + G) * a1
    a2p = (1 + G) * a2
    C1p = np.hypot(a1p, b1)
    C2p = np.hypot(a2p, b2)
    h1p = np.degrees(np.arctan2(b1, a1p)) % 360
    h2p = np.degrees(np.arctan2(b2, a2p)) % 360
    h1p = np.where((a1p == 0) & (b1 == 0), 0, h1p)
    h2p = np.where((a2p == 0) & (b2 == 0), 0, h2p)
    dLp = L2 - L1
    dCp = C2p - C1p
    dh = h2p - h1p
    dhp = np.where(C1p * C2p == 0, 0,
                   np.where(np.abs(dh) <= 180, dh,
                            np.where(dh > 180, dh - 360, dh + 360)))
    dHp = 2 * np.sqrt(C1p * C2p) * np.sin(np.radians(dhp / 2))
    Lbar = (L1 + L2) / 2
    Cbarp = (C1p + C2p) / 2
    hsum = h1p + h2p
    hbarp = np.where(C1p * C2p == 0, hsum,
                     np.where(np.abs(h1p - h2p) <= 180, hsum / 2,
                              np.where(hsum < 360, (hsum + 360) / 2, (hsum - 360) / 2)))
    T = (1 - 0.17 * np.cos(np.radians(hbarp - 30))
         + 0.24 * np.cos(np.radians(2 * hbarp))
         + 0.32 * np.cos(np.radians(3 * hbarp + 6))
         - 0.20 * np.cos(np.radians(4 * hbarp - 63)))
    dtheta = 30 * np.exp(-(((hbarp - 275) / 25.0) ** 2))
    RC = 2 * np.sqrt(Cbarp ** 7 / (Cbarp ** 7 + 25 ** 7 + 1e-30))
    SL = 1 + (0.015 * (Lbar - 50) ** 2) / np.sqrt(20 + (Lbar - 50) ** 2)
    SC = 1 + 0.045 * Cbarp
    SH = 1 + 0.015 * Cbarp * T
    RT = -np.sin(np.radians(2 * dtheta)) * RC
    return np.sqrt((dLp / SL) ** 2 + (dCp / SC) ** 2 + (dHp / SH) ** 2
                   + RT * (dCp / SC) * (dHp / SH))


def flat_mask(rgb, radius=1):
    """True where every pixel in a (2r+1)^2 window is identical."""
    h, w = rgb.shape[:2]
    mask = np.ones((h, w), dtype=bool)
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            if dx == 0 and dy == 0:
                continue
            shifted = np.roll(np.roll(rgb, dy, axis=0), dx, axis=1)
            mask &= np.all(rgb == shifted, axis=-1)
    return mask


def compare(rgb1, rgb2, flat=True, flat_radius=1):
    """Compare two sRGB renderings. Returns percentile stats in ΔE00."""
    h = min(rgb1.shape[0], rgb2.shape[0])
    w = min(rgb1.shape[1], rgb2.shape[1])
    a = rgb1[:h, :w].astype(np.uint8)
    b = rgb2[:h, :w].astype(np.uint8)
    lab1 = srgb_to_lab(a)
    lab2 = srgb_to_lab(b)
    de = delta_e00_array(lab1, lab2)
    mask = np.ones_like(de, dtype=bool)
    if flat:
        mask = flat_mask(a, radius=flat_radius) & flat_mask(b, radius=flat_radius)
    vals = de[mask]
    if vals.size == 0:
        vals = de.ravel()
    q = np.percentile(vals, [50, 90, 95, 99]).tolist()
    return {
        "width": w, "height": h,
        "pixels": int(vals.size),
        "median": round(float(q[0]), 3),
        "p90": round(float(q[1]), 3),
        "p95": round(float(q[2]), 3),
        "p99": round(float(q[3]), 3),
        "max": round(float(vals.max()), 3),
        "mean": round(float(vals.mean()), 3),
        "over2Percent": round(float((vals > 2).mean() * 100), 3),
        "over5Percent": round(float((vals > 5).mean() * 100), 3),
        "over10Percent": round(float((vals > 10).mean() * 100), 3),
        "flatOnly": bool(flat),
        "flatRadius": flat_radius,
        "colorSpace": "rendered sRGB, D65/2-degree, CIEDE2000",
    }


def _shift_pair(a, b, dx, dy):
    h, w = a.shape[:2]
    h2, w2 = b.shape[:2]
    # Positive dx moves b right; crop the non-overlapping part from both.
    ax0 = max(0, dx)
    ay0 = max(0, dy)
    bx0 = max(0, -dx)
    by0 = max(0, -dy)
    ww = min(w - ax0, w2 - bx0)
    hh = min(h - ay0, h2 - by0)
    return (a[ay0:ay0 + hh, ax0:ax0 + ww],
            b[by0:by0 + hh, bx0:bx0 + ww])


def compare_aligned(rgb1, rgb2, max_shift=2, flat=True, step=4, flat_radius=2):
    """Find the integer shift with the lowest mean ΔE, then compare."""
    best = None
    for dy in range(-max_shift, max_shift + 1):
        for dx in range(-max_shift, max_shift + 1):
            a, b = _shift_pair(rgb1, rgb2, dx, dy)
            if a.size == 0 or b.size == 0:
                continue
            aa = a[::step, ::step].astype(np.uint8)
            bb = b[::step, ::step].astype(np.uint8)
            de = delta_e00_array(srgb_to_lab(aa), srgb_to_lab(bb))
            score = float(de.mean())
            if best is None or score < best[0]:
                best = (score, dx, dy)
    if best is None:
        return compare(rgb1, rgb2, flat=flat, flat_radius=flat_radius)
    score, dx, dy = best
    a, b = _shift_pair(rgb1, rgb2, dx, dy)
    stats = compare(a, b, flat=flat, flat_radius=flat_radius)
    # Global ink coverage diff: flat-region ΔE alone misses thin lines and
    # small text, because those pixels are excluded as non-flat.
    ink_a = np.any(a < 250, axis=2)
    ink_b = np.any(b < 250, axis=2)
    stats["inkMismatchPercent"] = round(float((ink_a ^ ink_b).mean() * 100), 4)
    # Strong per-pixel difference over the whole image. Catches erased glyphs
    # on a coloured background, which the ink mask cannot see (both are ink).
    de_all = delta_e00_array(srgb_to_lab(a), srgb_to_lab(b))
    strong = de_all > 25
    stats["strongDiffPercent"] = round(float(strong.mean() * 100), 4)
    # Erode the strong-diff mask: anti-aliased glyph edges are one pixel wide,
    # an erased glyph leaves a solid blob. This separates real content loss
    # from rasteriser noise.
    if strong.shape[0] > 2 and strong.shape[1] > 2:
        core = (strong[1:-1, 1:-1] & strong[:-2, 1:-1] & strong[2:, 1:-1]
                & strong[1:-1, :-2] & strong[1:-1, 2:])
        stats["strongBlobPercent"] = round(float(core.mean() * 100), 4)
    else:
        stats["strongBlobPercent"] = 0.0
    stats["alignment"] = {"dx": dx, "dy": dy, "meanAtAlignment": round(score, 3)}
    return stats


def write_difference_map(path, rgb1, rgb2, threshold=5.0):
    """Magenta where predicted visual difference exceeds threshold."""
    h = min(rgb1.shape[0], rgb2.shape[0])
    w = min(rgb1.shape[1], rgb2.shape[1])
    de = delta_e00_array(srgb_to_lab(rgb1[:h, :w]), srgb_to_lab(rgb2[:h, :w]))
    out = np.zeros((h, w, 3), dtype=np.uint8)
    out[..., 0] = np.where(de > threshold, 255, 245)
    out[..., 1] = np.where(de > threshold, 0, 245)
    out[..., 2] = np.where(de > threshold, 255, 245)
    write_png(path, out)
