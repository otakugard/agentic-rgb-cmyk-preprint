#!/usr/bin/env python3
"""01a2-whiten-alpha-masks.py [--detect] <source.pdf> [<out.pdf>] <report.json>

Whiten the fill of Figma's rectangular `/S /Alpha` clip masks, keeping
`/S /Alpha` unchanged.

Why
---
Illustrator imports PDF alpha masks using the mask's colour as a
luminosity value. Figma's alpha clip rectangles are filled black, so the
masked panel is treated as black/transparent. Changing only the fill to
white keeps the alpha shape and clipping semantics intact, but makes
Illustrator's interpretation opaque where the mask says visible.

This is a colour-only change inside the mask: the alpha channel, the
rectangle geometry, the page content and the artwork colours are not
touched.

Guard
-----
Only masks whose `/G` form paints exactly one axis-aligned rectangle with
one unambiguous solid fill are rewritten. The rectangle test rejects
bow-ties and other 4-point self-intersections. Any unsupported alpha mask
stops the run; no partial PDF is published.

Requires pypdf (installed in the user site for this workspace).
"""

import hashlib
import json
import os
import re
import sys

try:
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import (ArrayObject, DictionaryObject, IndirectObject,
                               NameObject)
except ImportError:
    print("pypdf is required: python3 -m pip install --user --break-system-packages pypdf", file=sys.stderr)
    sys.exit(78)


WHITE_BY_N = {
    1: b"1",
    3: b"1 1 1",
    4: b"0 0 0 0",
}
BLACK_BY_N = {
    1: b"0",
    3: b"0 0 0",
    4: b"0 0 0 1",
}
GROUP_CS = {
    1: "/DeviceGray",
    3: "/DeviceRGB",
    4: "/DeviceCMYK",
}


def rect_from_ops(data):
    """Return (x0, y0, x1, y1) for one axis-aligned rectangle, else None.

    A 4-point path must walk the corners in order; a 5-point path may close
    back to the first point. This rejects bow-ties, which have two x and two
    y values but are not axis-aligned rectangles.
    """
    text = data.decode("latin1", "replace")
    tokens = text.split()
    if any(tok in tokens for tok in ("Do", "sh", "BT", "BI", "re", "S", "s")):
        return None
    if tokens.count("f") + tokens.count("f*") != 1:
        return None
    ops = re.findall(r"([-+]?\d*\.?\d+)\s+([-+]?\d*\.?\d+)\s+(m|l)\b", text)
    if len(ops) not in (4, 5):
        return None
    if sum(1 for _, _, op in ops if op == "m") != 1:
        return None
    pts = [(round(float(x), 3), round(float(y), 3)) for x, y, _ in ops]
    if len(pts) == 5:
        if pts[4] != pts[0]:
            return None
        pts = pts[:4]
    p0, p1, p2, p3 = pts
    xs = {p0[0], p1[0], p2[0], p3[0]}
    ys = {p0[1], p1[1], p2[1], p3[1]}
    if len(xs) != 2 or len(ys) != 2:
        return None
    # Consecutive corners must share one coordinate, and opposite corners
    # must differ in both. This is the rectangle walk, not a bow-tie.
    def shares(a, b):
        return a[0] == b[0] or a[1] == b[1]

    if not (shares(p0, p1) and shares(p1, p2) and shares(p2, p3) and shares(p3, p0)):
        return None
    horizontal_first = (p0[1] == p1[1] and p1[0] == p2[0] and
                        p2[1] == p3[1] and p3[0] == p0[0])
    vertical_first = (p0[0] == p1[0] and p1[1] == p2[1] and
                      p2[0] == p3[0] and p3[1] == p0[1])
    if horizontal_first or vertical_first:
        return min(xs), min(ys), max(xs), max(ys)
    return None


def analyse_fill(data):
    """Return (ncomp, op, cs_name) for exactly one non-stroking solid fill."""
    text = data.decode("latin1", "replace")
    tokens = text.split()
    fill_ops = [t for t in ("scn", "sc", "rg", "k", "g") if t in tokens]
    if len(fill_ops) != 1:
        return None
    op = fill_ops[0]
    if tokens.count(op) != 1:
        return None
    m = re.search(rb"((?:[-+]?\d*\.?\d+\s+)+)" + op.encode("ascii") + rb"\b", data)
    if not m:
        return None
    nums = m.group(1).split()
    if op in ("scn", "sc"):
        ncomp = len(nums)
    elif op == "rg":
        ncomp = 3
    elif op == "k":
        ncomp = 4
    else:
        ncomp = 1
    if ncomp not in (1, 3, 4):
        return None
    # If the stream names a device colour space, it must match the operand
    # count. A named ICC space (e.g. /C1) is inferred from the operand count.
    cs_name = None
    if "cs" in tokens:
        idx = tokens.index("cs")
        if idx > 0 and tokens[idx - 1].startswith("/"):
            cs_name = tokens[idx - 1]
    expected = {"/DeviceGray": 1, "/DeviceRGB": 3, "/DeviceCMYK": 4}.get(cs_name)
    if expected is not None and expected != ncomp:
        return None
    return ncomp, op, cs_name


def white_fill(data, ncomp, op):
    """Replace the single fill with white using the same operator family."""
    if op in ("scn", "sc"):
        value = WHITE_BY_N[ncomp]
        return re.sub(rb"((?:[-+]?\d*\.?\d+\s+)+)" + op.encode("ascii") + rb"\b",
                      value + b" " + op.encode("ascii"), data, count=1)
    if op == "rg":
        return re.sub(rb"((?:[-+]?\d*\.?\d+\s+)+)rg\b", b"1 1 1 rg", data, count=1)
    if op == "k":
        return re.sub(rb"((?:[-+]?\d*\.?\d+\s+)+)k\b", b"0 0 0 0 k", data, count=1)
    if op == "g":
        return re.sub(rb"((?:[-+]?\d*\.?\d+\s+)+)g\b", b"1 g", data, count=1)
    return None


def black_bbox(bbox, ncomp):
    if bbox is None or len(bbox) != 4:
        return None
    x0, y0, x1, y1 = [float(v) for v in bbox]
    op = {1: b"g", 3: b"scn", 4: b"scn"}[ncomp]
    value = BLACK_BY_N[ncomp]
    return ("%s %s\n%f %f %f %f re\nf\n" % (
        value.decode("ascii"), op.decode("ascii"), x0, y0, x1 - x0, y1 - y0)).encode("ascii")


def set_group_cs(g, ncomp):
    grp = g.get("/Group")
    if grp is None:
        grp = DictionaryObject()
        grp[NameObject("/Type")] = NameObject("/Group")
        grp[NameObject("/S")] = NameObject("/Transparency")
        g[NameObject("/Group")] = grp
    else:
        grp = grp.get_object()
    grp[NameObject("/CS")] = NameObject(GROUP_CS[ncomp])


def safe_paths(src, dst, report_path):
    src_real = os.path.realpath(src)
    for label, path in (("output", dst), ("report", report_path)):
        if path is None:
            continue
        real = os.path.realpath(path)
        if real == src_real:
            raise SystemExit("%s path must not equal the source PDF" % label)
        if os.path.basename(os.path.dirname(real)) != "work":
            raise SystemExit("%s must be written inside a directory named work: %s" % (label, path))
    if dst is not None and os.path.dirname(os.path.realpath(dst)) != os.path.dirname(os.path.realpath(report_path)):
        raise SystemExit("output and report must be in the same work directory")


def main():
    detect_only = len(sys.argv) > 1 and sys.argv[1] == "--detect"
    args = sys.argv[2:] if detect_only else sys.argv[1:]
    if (detect_only and len(args) != 2) or (not detect_only and len(args) != 3):
        print("usage: 01a2-alpha-to-luminosity.py [--detect] <source.pdf> [<out.pdf>] <report.json>", file=sys.stderr)
        return 64
    if detect_only:
        src, report_path = args
        dst = None
    else:
        src, dst, report_path = args
    safe_paths(src, dst, report_path)

    raw = open(src, "rb").read()
    report = {
        "ok": False,
        "step": "01a2-alpha-to-luminosity",
        "mode": "detect" if detect_only else "rewrite",
        "input": src,
        "inputSha256": hashlib.sha256(raw).hexdigest(),
        "output": dst,
        "detected": 0,
        "converted": [],
        "skipped": [],
        "reason": None,
    }

    reader = PdfReader(src)
    writer = PdfWriter()
    writer.append(reader)

    seen_indirect = set()
    seen_inline = set()
    seen_masks = set()
    masks = []

    def add_mask(mask, where):
        if isinstance(mask, IndirectObject):
            mask = mask.get_object()
        if not isinstance(mask, DictionaryObject):
            report["skipped"].append({"where": where, "why": "mask is not a dictionary"})
            return
        kind = mask.get("/S")
        if kind is None:
            report["skipped"].append({"where": where, "why": "mask has no /S"})
            return
        if kind != "/Alpha":
            # Already-luminosity masks (and other mask types) are not alpha
            # clip masks and are intentionally outside this rewrite.
            return
        if id(mask) in seen_masks:
            return
        seen_masks.add(id(mask))
        masks.append((mask, where))

    def walk(obj):
        if isinstance(obj, IndirectObject):
            key = (obj.idnum, obj.generation)
            if key in seen_indirect:
                return
            seen_indirect.add(key)
            walk(obj.get_object())
            return
        if isinstance(obj, DictionaryObject):
            if id(obj) in seen_inline:
                return
            seen_inline.add(id(obj))
            # A mask may be referenced by an ExtGState /SMask, or be a
            # standalone mask dictionary. Inspect both.
            if "/SMask" in obj:
                add_mask(obj["/SMask"], "/SMask")
            if obj.get("/S") == "/Alpha" and "/G" in obj:
                add_mask(obj, "mask-dict")
            for v in list(obj.values()):
                walk(v)
        elif isinstance(obj, ArrayObject):
            for v in obj:
                walk(v)

    for obj in list(writer._objects):
        walk(obj)

    # Deduplicate by object id after the walk.
    unique = []
    seen = set()
    for mask, where in masks:
        if id(mask) in seen:
            continue
        seen.add(id(mask))
        unique.append((mask, where))
    masks = unique
    report["detected"] = len(masks)

    for mask, where in masks:
        if mask.get("/S") != "/Alpha":
            report["skipped"].append({"where": where, "why": "not /S /Alpha"})
            continue
        g = mask.get("/G")
        if g is None:
            report["skipped"].append({"where": where, "why": "alpha mask without /G"})
            continue
        g = g.get_object()
        if not isinstance(g, DictionaryObject) or g.get("/Subtype") != "/Form":
            report["skipped"].append({"where": where, "why": "mask /G is not a Form XObject"})
            continue
        data = g.get_data()
        rect = rect_from_ops(data)
        if rect is None:
            report["skipped"].append({"where": where, "g": getattr(g, "idnum", None),
                                      "why": "mask is not one axis-aligned solid rectangle"})
            continue
        fill = analyse_fill(data)
        if fill is None:
            report["skipped"].append({"where": where, "g": getattr(g, "idnum", None),
                                      "why": "fill colour operator is not unambiguous"})
            continue
        ncomp, op, cs_name = fill
        if detect_only:
            report["converted"].append({
                "where": where,
                "g": getattr(g, "idnum", None),
                "rect": [round(v, 3) for v in rect],
                "components": ncomp,
                "operator": op,
                "cs": cs_name,
                "from": "/Alpha",
                "to": "/Luminosity",
            })
            continue
        new_data = white_fill(data, ncomp, op)
        if new_data is None:
            report["skipped"].append({"where": where, "g": getattr(g, "idnum", None),
                                      "why": "white fill could not be written"})
            continue
        g.set_data(new_data)
        report["converted"].append({
            "where": where,
            "g": getattr(g, "idnum", None),
            "rect": [round(v, 3) for v in rect],
            "components": ncomp,
            "operator": op,
            "cs": cs_name,
            "from": "/Alpha",
            "to": "/Alpha",
            "fill": "white",
        })

    if report["skipped"]:
        report["reason"] = ("at least one alpha mask is not a supported solid rectangle; "
                            "no output was written")
        json.dump(report, open(report_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(report["reason"], file=sys.stderr)
        print(json.dumps(report["skipped"], ensure_ascii=False), file=sys.stderr)
        return 65

    if detect_only:
        report["ok"] = True
        report["reason"] = ("alpha clip masks detected" if report["detected"] else "no alpha masks found")
        json.dump(report, open(report_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(json.dumps({
            "ok": True,
            "detected": report["detected"],
            "supported": len(report["converted"]),
            "report": report_path,
        }, ensure_ascii=False))
        return 65 if report["detected"] else 0

    # Publish only after every mask passed validation.
    tmp = dst + ".tmp"
    writer.write(tmp)
    os.replace(tmp, dst)
    report["ok"] = True
    report["outputSha256"] = hashlib.sha256(open(dst, "rb").read()).hexdigest()
    report["reason"] = ("whitened rectangular alpha clip mask fills, /S /Alpha unchanged"
                        if report["converted"] else "no alpha masks found")
    json.dump(report, open(report_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(json.dumps({
        "ok": True,
        "converted": len(report["converted"]),
        "skipped": len(report["skipped"]),
        "output": dst,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
