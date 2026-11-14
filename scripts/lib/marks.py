#!/usr/bin/env python3
"""Japanese double tombo (jp-double style) for the delivery PDF.

Pure geometry: no PDF objects, no file IO. 07c writes the returned content
stream; Gate3 imports the same module to verify the result.

Parameters (all in points unless the key ends in Mm):
  offsetMm      gap between the trim edge and the outer tombo (3 mm)
  outerMm       outer end of the corner tombo (12 mm from trim)
  lineWidthPt   stroke width (0.3 pt)
  crossLongPt   centre cross arm parallel to the edge (72 pt)
  crossShortPt  centre cross arm perpendicular to the edge (24 pt)
  crossDistPt   centre cross intersection from the trim edge (18 pt)
"""

DEFAULTS = {
    "style": "jp-double",
    "source": "drawn",
    "offsetMm": 3.0,
    "outerMm": 12.0,
    "lineWidthPt": 0.3,
    "crossLongPt": 72.0,
    "crossShortPt": 24.0,
    "crossDistPt": 18.0,
    "colorBars": False,
}

MM = 72.0 / 25.4


def config(raw=None):
    cfg = dict(DEFAULTS)
    cfg.update(raw or {})
    if cfg["style"] != "jp-double":
        raise ValueError("marks.style must be 'jp-double' (got %r)" % cfg["style"])
    if cfg["colorBars"]:
        raise ValueError("marks.colorBars must be false for jp-double")
    return cfg


def segments(trim, raw=None):
    """Return the 24 line segments as ((x1, y1), (x2, y2))."""
    cfg = config(raw)
    x0, y0, x1, y1 = [float(v) for v in trim]
    off = float(cfg["offsetMm"]) * MM
    outer = float(cfg["outerMm"]) * MM
    long_arm = float(cfg["crossLongPt"])
    short_arm = float(cfg["crossShortPt"])
    dist = float(cfg["crossDistPt"])
    midx = (x0 + x1) / 2.0
    midy = (y0 + y1) / 2.0
    out = []

    for sx in (-1, 1):
        for sy in (-1, 1):
            cx = x0 if sx < 0 else x1
            cy = y0 if sy < 0 else y1
            # inner tombo: on the trim edges, 12 mm -> 3 mm outside
            out.append(((cx + sx * outer, cy), (cx + sx * off, cy)))
            out.append(((cx, cy + sy * outer), (cx, cy + sy * off)))
            # outer tombo: on the 3 mm bleed line, 12 mm outside -> trim
            out.append(((cx + sx * off, cy + sy * outer), (cx + sx * off, cy)))
            out.append(((cx + sx * outer, cy + sy * off), (cx, cy + sy * off)))

    # centre crosses: long arm parallel to the edge, short arm perpendicular
    out.append(((x0 - long_arm / 2, midy), (x0 - short_arm / 2, midy)))
    out.append(((x0 - dist, midy - long_arm / 2), (x0 - dist, midy + long_arm / 2)))
    out.append(((x1 + short_arm / 2, midy), (x1 + long_arm / 2, midy)))
    out.append(((x1 + dist, midy - long_arm / 2), (x1 + dist, midy + long_arm / 2)))
    out.append(((midx - long_arm / 2, y0 - dist), (midx + long_arm / 2, y0 - dist)))
    out.append(((midx, y0 - long_arm / 2), (midx, y0 - short_arm / 2)))
    out.append(((midx - long_arm / 2, y1 + dist), (midx + long_arm / 2, y1 + dist)))
    out.append(((midx, y1 + short_arm / 2), (midx, y1 + long_arm / 2)))
    return out


def content(trim, raw=None):
    """Return the PDF content stream that strokes the marks."""
    cfg = config(raw)
    lines = ["q", "/CSAll CS", "1 SCN",
             "%.2f w 4 M" % float(cfg["lineWidthPt"])]
    for (ax, ay), (bx, by) in segments(trim, cfg):
        lines.append("%.4f %.4f m %.4f %.4f l" % (ax, ay, bx, by))
    lines.extend(["S", "Q", ""])
    return "\n".join(lines).encode("ascii")


def selftest():
    import itertools
    for trim in ((39.685, 39.685, 459.213, 634.961),
                 (39.685, 39.685, 881.575, 634.961)):
        segs = segments(trim)
        assert len(segs) == 24, len(segs)
        x0, y0, x1, y1 = trim
        for (ax, ay), (bx, by) in segs:
            for x, y in ((ax, ay), (bx, by)):
                assert x0 - 40 <= x <= x1 + 40 and y0 - 40 <= y <= y1 + 40
                # no mark point may fall inside the trim rectangle
                assert not (x0 + 0.01 < x < x1 - 0.01
                            and y0 + 0.01 < y < y1 - 0.01), (x, y)
        # inner/outer tombo spacing == offset
        off = DEFAULTS["offsetMm"] * MM
        pairs = 0
        for a, b in itertools.combinations(segs, 2):
            if abs(a[0][0] - x0) < 0.01 and abs(b[0][0] - (x0 - off)) < 0.01:
                pairs += 1
        assert pairs > 0
    return True


if __name__ == "__main__":
    import sys
    print("marks selftest:", "ok" if selftest() else "fail")
    sys.exit(0 if selftest() else 1)
