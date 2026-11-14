#!/usr/bin/env python3
"""01a-source-audit.py <source.pdf> <work-dir>

Reads the actual Figma PDF, extracts its ICC profile and exact colour
operators, and classifies the input. It never writes to source/.

Output: <work-dir>/source-audit.json plus source-icc.icc when present.
"""

import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "lib"))
import pdfprobe  # noqa: E402


def main():
    if len(sys.argv) != 3:
        print("usage: 01a-source-audit.py <source.pdf> <work-dir>", file=sys.stderr)
        return 64
    src, work = sys.argv[1], sys.argv[2]
    if not os.path.exists(src):
        print("source not found: %s" % src, file=sys.stderr)
        return 66
    os.makedirs(work, exist_ok=True)

    raw = open(src, "rb").read()
    objs = pdfprobe.load_object_streams(pdfprobe.load_objects(raw))
    pdfprobe.resolve_streams(raw, objs)
    profiles = pdfprobe.extract_icc_profiles(raw, objs)
    boxes = pdfprobe.page_boxes(raw, objs)
    structure = pdfprobe.count_structure(objs)
    colours = pdfprobe.scan_colours(objs)

    icc_path = None
    rgb_profiles = [p for p in profiles if p.get("isRGB")]
    cmyk_profiles = [p for p in profiles if p.get("isCMYK")]
    if rgb_profiles:
        icc_path = os.path.join(work, "source-icc.icc")
        with open(icc_path, "wb") as fh:
            fh.write(rgb_profiles[0]["data"])
    if cmyk_profiles:
        with open(os.path.join(work, "source-icc-cmyk.icc"), "wb") as fh:
            fh.write(cmyk_profiles[0]["data"])

    has_cmyk = (structure["deviceCMYK"] > 0 or bool(cmyk_profiles)
                or any(c.get("space") == "cmyk" for c in colours))
    has_rgb = structure["deviceRGB"] > 0 or bool(rgb_profiles)
    if has_cmyk and has_rgb:
        kind = "mixed-rgb-cmyk"
    elif has_cmyk:
        kind = "cmyk"
    elif has_rgb and rgb_profiles:
        kind = "rgb-icc"
    elif has_rgb:
        kind = "rgb-untagged"
    else:
        kind = "unknown"

    payload = {
        "ok": kind == "rgb-icc" and not has_cmyk,
        "step": "01a-source-audit",
        "source": os.path.basename(src),
        "sourceSha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "header": raw[:9].decode("latin1", "replace").strip(),
        "pages": len(pdfprobe.re.findall(rb"/Type\s*/Page[^s]", raw)),
        "boxes": boxes,
        "kind": kind,
        "hasRGB": has_rgb,
        "hasCMYK": has_cmyk,
        "icc": ({k: v for k, v in rgb_profiles[0].items() if k != "data"}
                if rgb_profiles else None),
        "iccPath": os.path.relpath(icc_path, os.path.dirname(work)) if icc_path else None,
        "cmykIcc": ({k: v for k, v in cmyk_profiles[0].items() if k != "data"}
                    if cmyk_profiles else None),
        "structure": structure,
        "colours": colours,
        "vectorMaxTac": pdfprobe.vector_max_tac(objs),
        "note": "Operator counts are content-stream occurrences, not printed area.",
    }
    out = os.path.join(work, "source-audit.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)

    print(json.dumps({
        "ok": payload["ok"], "kind": kind, "pages": payload["pages"],
        "icc": (payload["icc"] or {}).get("description"),
        "iccSha256": (payload["icc"] or {}).get("sha256"),
        "hasCMYK": has_cmyk, "colours": len(colours),
        "vectorMaxTac": payload["vectorMaxTac"],
    }, ensure_ascii=False))
    if payload["pages"] != 1:
        return 65
    if kind != "rgb-icc":
        return 65
    return 0


if __name__ == "__main__":
    sys.exit(main())
