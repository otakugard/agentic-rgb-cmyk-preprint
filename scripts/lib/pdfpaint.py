#!/usr/bin/env python3
"""Parse PDF paint events and verify the brand-colour region contract.

This is a bounded content-stream reader, not a renderer. It tracks graphics
state, CTM, path bounds, fill colour, colour space, opacity, blend mode,
overprint and Form invocations. It answers one question: which stored paint
operation covers each declared brand region, and what ink does it store?

Renderer plate values are checked separately with an explicit output ICC.
"""

import math
import os
import sys

from pypdf import PdfReader
from pypdf.generic import ContentStream

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import color  # noqa: E402
import contract as colorcontract  # noqa: E402


def _num(value):
    try:
        return float(value)
    except Exception:  # noqa: BLE001
        return 0.0


def _mul(m, n):
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (a * A + c * B, b * A + d * B,
            a * C + c * D, b * C + d * D,
            a * E + c * F + e, b * E + d * F + f)


def _pt(m, x, y):
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


def _bbox_add(box, m, x, y):
    X, Y = _pt(m, x, y)
    if box is None:
        return [X, Y, X, Y]
    return [min(box[0], X), min(box[1], Y),
            max(box[2], X), max(box[3], Y)]


def _bbox_union(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return [min(a[0], b[0]), min(a[1], b[1]),
            max(a[2], b[2]), max(a[3], b[3])]


def _cs_name(obj):
    return str(obj).lstrip("/") if obj is not None else None


def _resolve_cs(name, resources):
    if name is None:
        return None
    cs = (resources or {}).get("/ColorSpace")
    cs = cs.get_object() if hasattr(cs, "get_object") else (cs or {})
    obj = cs.get("/" + str(name).lstrip("/"))
    if obj is None:
        return _cs_name(name)
    obj = obj.get_object()
    if isinstance(obj, list) and obj:
        head = _cs_name(obj[0])
        if head == "ICCBased" and len(obj) > 1:
            stream = obj[1].get_object()
            return "ICCBased%d" % int(stream.get("/N", 3))
        if head == "Separation" and len(obj) > 1:
            return "Separation:" + _cs_name(obj[1])
        if head == "DeviceN" and len(obj) > 1:
            names = obj[1]
            return "DeviceN%d" % len(names)
        return head
    return _cs_name(obj)


def _paint_from_operands(space, ops):
    if space in ("DeviceCMYK", "ICCBased4"):
        if len(ops) >= 4:
            return ("cmyk", tuple(round(_num(x) * 100.0, 4) for x in ops[:4]))
    if space in ("DeviceRGB", "ICCBased3", "CalRGB"):
        if len(ops) >= 3:
            return ("rgb", tuple(round(_num(x) * 255.0) for x in ops[:3]))
    if space == "DeviceGray":
        if ops:
            return ("gray", (round(_num(ops[0]) * 100.0, 2),))
    if space and space.startswith("Separation:"):
        return ("spot", (space.split(":", 1)[1],
                         round(_num(ops[0]), 4) if ops else None))
    if space and space.startswith("DeviceN"):
        return ("devicen", tuple(round(_num(x), 4) for x in ops))
    return ("unknown", tuple(_num(x) for x in ops))


def extract_fill_events(path, max_depth=8):
    reader = PdfReader(path)
    page = reader.pages[0]
    events = []
    unsupported = []

    def walk(stream, resources, page_resources, gs, scope, depth):
        stack = []
        path_box = None
        path_points = []
        path_curves = 0
        path_ops = 0
        for operands, operator in stream.operations:
            op = operator.decode("latin1") if isinstance(operator, bytes) else str(operator)
            if op == "q":
                stack.append(dict(gs))
            elif op == "Q":
                if stack:
                    gs = stack.pop()
            elif op == "cm" and len(operands) == 6:
                gs["ctm"] = _mul(gs["ctm"], tuple(_num(x) for x in operands))
            elif op in ("m", "l") and len(operands) >= 2:
                path_ops += 1
                x, y = _num(operands[0]), _num(operands[1])
                path_box = _bbox_add(path_box, gs["ctm"], x, y)
                path_points.append(_pt(gs["ctm"], x, y))
            elif op in ("c", "v", "y") and len(operands) >= 4:
                path_ops += 1
                path_curves += 1
                for i in range(0, len(operands) - 1, 2):
                    x, y = _num(operands[i]), _num(operands[i + 1])
                    path_box = _bbox_add(path_box, gs["ctm"], x, y)
                    path_points.append(_pt(gs["ctm"], x, y))
            elif op == "re" and len(operands) >= 4:
                path_ops += 1
                x, y, w, h = [_num(v) for v in operands[:4]]
                for px, py in ((x, y), (x + w, y), (x + w, y + h), (x, y + h)):
                    path_box = _bbox_add(path_box, gs["ctm"], px, py)
                    path_points.append(_pt(gs["ctm"], px, py))
            elif op in ("W", "W*"):
                gs["clip"] = True
            elif op == "h":
                pass
            elif op in ("f", "F", "f*", "B", "B*", "b", "b*"):
                if gs.get("fill"):
                    kind, vals = gs["fill"]
                    axis_rect = False
                    if path_box and path_points and not path_curves:
                        corners = {(round(x, 3), round(y, 3)) for x, y in path_points}
                        bbox_corners = {(round(path_box[0], 3), round(path_box[1], 3)),
                                        (round(path_box[2], 3), round(path_box[1], 3)),
                                        (round(path_box[2], 3), round(path_box[3], 3)),
                                        (round(path_box[0], 3), round(path_box[3], 3))}
                        axis_rect = len(corners) == 4 and corners == bbox_corners
                    events.append({
                        "scope": scope, "op": op, "kind": kind, "values": vals,
                        "bbox": path_box, "ctm": gs["ctm"],
                        "ca": gs.get("ca"), "blend": gs.get("blend"),
                        "smask": gs.get("smask"),
                        "overprint": bool(gs.get("op") or gs.get("OP")),
                        "opm": gs.get("opm"), "colorspace": gs.get("cs"),
                        "groupCS": gs.get("groupCS"),
                        "clipped": bool(gs.get("clip")),
                        "pathOps": path_ops, "pathCurves": path_curves,
                        "isAxisAlignedRect": axis_rect,
                    })
                path_box = None
                path_points = []
                path_curves = 0
                path_ops = 0
            elif op in ("n", "S", "s"):
                path_box = None
                path_points = []
                path_curves = 0
                path_ops = 0
            elif op in ("k", "K") and len(operands) >= 4:
                if op == "k":
                    gs["cs"] = "DeviceCMYK"
                    gs["fill"] = ("cmyk", tuple(round(_num(x) * 100.0, 4)
                                               for x in operands[:4]))
            elif op in ("rg", "RG") and len(operands) >= 3:
                if op == "rg":
                    gs["cs"] = "DeviceRGB"
                    gs["fill"] = ("rgb", tuple(round(_num(x) * 255.0)
                                               for x in operands[:3]))
            elif op in ("g", "G") and operands:
                if op == "g":
                    gs["cs"] = "DeviceGray"
                    gs["fill"] = ("gray", (round(_num(operands[0]) * 100.0, 2),))
            elif op in ("cs", "CS") and operands:
                if op == "cs":
                    gs["cs"] = _resolve_cs(operands[0], resources)
                    # A colour-space change resets the current nonstroking colour.
                    gs["fill"] = None
            elif op in ("sc", "scn", "SC", "SCN"):
                if op in ("sc", "scn"):
                    gs["fill"] = _paint_from_operands(gs.get("cs"), operands)
            elif op == "gs" and operands:
                egs = (resources or {}).get("/ExtGState")
                egs = egs.get_object() if hasattr(egs, "get_object") else (egs or {})
                st = egs.get(operands[0])
                st = st.get_object() if hasattr(st, "get_object") else st
                if st:
                    if "/ca" in st:
                        gs["ca"] = _num(st["/ca"])
                    if "/CA" in st:
                        gs["CA"] = _num(st["/CA"])
                    if "/BM" in st:
                        gs["blend"] = _cs_name(st["/BM"])
                    if "/SMask" in st:
                        smask = _cs_name(st["/SMask"])
                        gs["smask"] = None if smask in (None, "None") else smask
                    if "/op" in st:
                        gs["op"] = bool(st["/op"])
                    if "/OP" in st:
                        gs["OP"] = bool(st["/OP"])
                    if "/OPM" in st:
                        gs["opm"] = int(_num(st["/OPM"]))
            elif op == "Do" and operands:
                if depth >= max_depth:
                    unsupported.append({"scope": scope, "type": "form_depth",
                                        "name": str(operands[0])})
                    continue
                xo = (resources or {}).get("/XObject")
                xo = xo.get_object() if hasattr(xo, "get_object") else (xo or {})
                ref = xo.get("/" + str(operands[0]).lstrip("/"))
                if ref is None:
                    continue
                obj = ref.get_object()
                subtype = str(obj.get("/Subtype"))
                if subtype != "/Form":
                    if subtype == "/Image":
                        unsupported.append({"scope": scope, "type": "image",
                                            "name": str(operands[0])})
                    continue
                m = obj.get("/Matrix") or [1, 0, 0, 1, 0, 0]
                fm = tuple(_num(x) for x in m)
                fres = obj.get("/Resources")
                # A Form without /Resources inherits the page resources under
                # PDF 1.6; an explicitly empty dictionary does not.
                fres = fres.get_object() if fres is not None else page_resources
                sub = dict(gs)
                sub["ctm"] = _mul(gs["ctm"], fm)
                group = obj.get("/Group")
                if group is not None:
                    group = group.get_object()
                    sub["groupCS"] = _cs_name(group.get("/CS"))
                walk(ContentStream(obj, reader), fres, page_resources, sub,
                     scope + "/" + str(operands[0]).lstrip("/"), depth + 1)
            elif op in ("BI", "INLINE IMAGE"):
                unsupported.append({"scope": scope, "type": "inline_image"})
            elif op == "sh":
                unsupported.append({"scope": scope, "type": "shading"})

    page_res = page.get("/Resources")
    page_res = page_res.get_object() if page_res is not None else {}
    walk(ContentStream(page.get_contents(), reader),
         page_res, page_res,
         {"ctm": (1, 0, 0, 1, 0, 0), "fill": None, "cs": None,
          "ca": 1, "CA": 1, "blend": "Normal", "smask": None,
          "op": False, "OP": False, "opm": 0, "groupCS": None,
          "clip": False},
         "page", 0)
    return reader, page, events, unsupported


def _region_rect(region, bleed_box):
    """Map a Figma bbox (top-left origin) onto the PDF BleedBox."""
    x0, y0, x1, y1 = [float(v) for v in bleed_box]
    fw, fh = [float(v) for v in region["framePx"]]
    bx0, by0, bx1, by1 = [float(v) for v in region["bboxPx"]]
    return [
        x0 + bx0 / fw * (x1 - x0),
        y1 - by1 / fh * (y1 - y0),
        x0 + bx1 / fw * (x1 - x0),
        y1 - by0 / fh * (y1 - y0),
    ]


def _contains(outer, inner, tolerance=1.0):
    if not outer or not inner:
        return False
    return (outer[0] <= inner[0] + tolerance and outer[1] <= inner[1] + tolerance
            and outer[2] >= inner[2] - tolerance and outer[3] >= inner[3] - tolerance)


def _area(box):
    if not box:
        return 0.0
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _overlap(a, b):
    if not a or not b:
        return 0.0
    return (max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
            * max(0.0, min(a[3], b[3]) - max(a[1], b[1])))


def _lab(profile, cmyk):
    return color.cmyk_to_lab([tuple(cmyk)], intent=1, src_profile=profile)[0]


def _greenish(profile, cmyk, cache=None):
    key = tuple(round(float(x), 2) for x in cmyk)
    if cache is not None and key in cache:
        return cache[key]
    L, a, b = _lab(profile, cmyk)
    chroma = math.hypot(a, b)
    hue = math.degrees(math.atan2(b, a)) % 360 if (a or b) else 0.0
    result = {"lab": [round(L, 2), round(a, 2), round(b, 2)],
              "chroma": round(chroma, 2), "hue": round(hue, 1),
              "isGreen": 100.0 <= hue <= 200.0 and chroma >= 8.0}
    if cache is not None:
        cache[key] = result
    return result


def _close(cmyk, target, tolerance):
    return all(abs(float(a) - float(b)) <= tolerance for a, b in zip(cmyk, target))


def brand_green_check(pdf, contract, job, boxes, target_icc):
    """Verify brand-colour paint events against the declared regions.

    Strict rules:
    - the canonical event must be DeviceCMYK, opaque, Normal, non-overprinting;
    - it must cover each declared Figma region mapped to the BleedBox;
    - no second canonical/overlapping background event may cover the region;
    - every other greenish stored tuple must be the separate vegetarian colour;
    - an image or shading covering the region withholds PASS.
    """
    color_name = colorcontract.brand_color_name(contract)
    spec = contract["colors"][color_name]
    canonical = [float(x) for x in spec["cmyk"]["percent"]]
    tolerance = float(spec["cmyk"].get("maxPercentDeviation", 0.05))
    expected_bytes = [int(x) for x in spec["cmyk"]["bytes8"]]
    other_greens = [
        [float(x) for x in (entry.get("cmyk") or {}).get("percent") or []]
        for name, entry in (contract.get("colors") or {}).items()
        if name != color_name and (entry.get("cmyk") or {}).get("percent")
    ]
    bleed = boxes.get("BleedBox")
    result = {"status": "pending", "canonical": canonical,
              "expectedBytes": expected_bytes, "regions": [],
              "events": [], "greenTuples": [], "unsupported": []}
    if not bleed or len(bleed) != 4:
        result["error"] = "BleedBox missing"
        result["status"] = "fail"
        return result
    try:
        _, _, events, unsupported = extract_fill_events(pdf)
    except Exception as exc:  # noqa: BLE001
        result["error"] = "content parse failed: %s" % exc
        result["status"] = "fail"
        return result

    node_id = str((job.get("figma") or {}).get("nodeId") or "")
    regions = [r for r in spec["regions"] if str(r.get("jobNode")) == node_id]
    if not regions:
        result["error"] = "no contract region for Figma node %s" % node_id
        result["status"] = "fail"
        return result

    canonical_events = []
    for idx, ev in enumerate(events):
        if ev["kind"] != "cmyk" or not ev.get("bbox"):
            continue
        if _close(ev["values"], canonical, tolerance):
            ev["_index"] = idx
            canonical_events.append(ev)
    result["canonicalEventCount"] = len(canonical_events)
    result["events"] = [{k: ev.get(k) for k in
                         ("scope", "op", "values", "bbox", "ca", "blend",
                          "smask", "overprint", "opm", "colorspace", "groupCS",
                          "clipped", "pathOps", "pathCurves", "isAxisAlignedRect")}
                        for ev in canonical_events]

    ok = True
    for region in regions:
        expected = _region_rect(region, bleed)
        covering = [ev for ev in canonical_events if _contains(ev["bbox"], expected)]
        duplicate = [ev for ev in canonical_events
                     if ev not in covering and _overlap(ev["bbox"], expected) > _area(expected) * 0.01]
        row = {"label": region["label"], "expectedBbox": [round(x, 3) for x in expected],
               "covering": len(covering), "overlapping": len(duplicate),
               "coverageOk": bool(covering),
               "duplicateOk": len(covering) == 1 and not duplicate,
               "stateOk": True, "geometryOk": True,
               "coveredByLaterFill": False}
        for ev in covering:
            if not ev.get("isAxisAlignedRect") or ev.get("clipped"):
                row["geometryOk"] = False
                row["geometry"] = {"isAxisAlignedRect": ev.get("isAxisAlignedRect"),
                                   "clipped": ev.get("clipped"),
                                   "pathOps": ev.get("pathOps"),
                                   "pathCurves": ev.get("pathCurves")}
            if (ev.get("colorspace") != "DeviceCMYK"
                    or ev.get("ca") not in (1, 1.0)
                    or str(ev.get("blend") or "Normal") not in ("Normal", "NORMAL")
                    or ev.get("smask")
                    or ev.get("overprint")
                    or ev.get("opm") not in (0, None)
                    or ev.get("groupCS") not in (None, "DeviceCMYK")):
                row["stateOk"] = False
                row["state"] = {k: ev.get(k) for k in
                                ("colorspace", "ca", "blend", "smask", "overprint", "opm", "groupCS")}
            for later in events[ev["_index"] + 1:]:
                if later.get("kind") not in ("cmyk", "spot"):
                    continue
                if not later.get("bbox"):
                    continue
                if _overlap(later["bbox"], expected) < _area(expected) * 0.95:
                    continue
                if _close(later.get("values") or (), canonical, tolerance):
                    continue
                if later.get("ca") not in (1, 1.0):
                    continue
                if str(later.get("blend") or "Normal") not in ("Normal", "NORMAL"):
                    continue
                row["coveredByLaterFill"] = True
                row["laterFill"] = {"scope": later.get("scope"),
                                    "values": later.get("values"),
                                    "bbox": later.get("bbox")}
        if not (row["coverageOk"] and row["duplicateOk"] and row["stateOk"]
                and row["geometryOk"] and not row["coveredByLaterFill"]):
            ok = False
        result["regions"].append(row)

    if unsupported:
        result["unsupported"] = unsupported
        # An image/shading in the page is not automatically in a brand region;
        # report it and withhold PASS for this bounded check.
        ok = False

    # Green inventory: every greenish stored tuple must match a declared
    # contract colour. This catches a second near-green definition.
    cache = {}
    tuples = []
    seen = set()
    for ev in events:
        if ev["kind"] != "cmyk" or not ev.get("values"):
            continue
        key = tuple(round(float(x), 2) for x in ev["values"])
        if key in seen:
            continue
        seen.add(key)
        info = _greenish(target_icc, key, cache)
        if info["isGreen"]:
            tuples.append({"cmyk": list(key), "hue": info["hue"],
                           "chroma": info["chroma"],
                           "isCanonical": _close(key, canonical, tolerance),
                           "isDeclared": _close(key, canonical, tolerance)
                           or any(_close(key, g, tolerance) for g in other_greens)})
    result["greenTuples"] = tuples
    for item in tuples:
        if not item["isDeclared"]:
            ok = False
    if not any(item["isCanonical"] for item in tuples):
        ok = False

    result["status"] = "pass" if ok else "fail"
    return result
