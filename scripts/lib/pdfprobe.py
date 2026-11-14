#!/usr/bin/env python3
"""Minimal, dependency-free PDF reader used by the print pipeline.

It is not a full PDF parser. It reads indirect objects, decompresses
FlateDecode streams (including object streams), extracts ICC profiles,
colour operators and the structural facts that the print checks need.

The goal is to inspect the file that is actually on disk, not a
Ghostscript rewrite of it.
"""

import hashlib
import re
import struct
import zlib


OBJ_RE = re.compile(rb"(?m)(?:^|[\r\n])(\d+)[ \t]+(\d+)[ \t]+obj\b")
STREAM_RE = re.compile(rb"\bstream\r?\n")
ENDOBJ_RE = re.compile(rb"\bendobj\b")


def _find_stream(raw, start):
    """Return (dict_bytes, stream_start) for an object body starting at start."""
    m = STREAM_RE.search(raw, start)
    end = ENDOBJ_RE.search(raw, start)
    if not m or (end and end.start() < m.start()):
        stop = end.start() if end else min(len(raw), start + 20000)
        return raw[start:stop], None
    return raw[start:m.start()], m.end()


def _length(dict_bytes, objs_raw):
    m = re.search(rb"/Length\s+(\d+)\s+(\d+)\s+R", dict_bytes)
    if m:
        ref = int(m.group(1))
        target = objs_raw.get(ref)
        if target:
            n = re.search(rb"(\d+)", target["dict"])
            if n:
                return int(n.group(1))
        return None
    m = re.search(rb"/Length\s+(\d+)", dict_bytes)
    return int(m.group(1)) if m else None


def load_objects(raw):
    """Return {objnum: {'dict': bytes, 'stream': bytes|None}}.

    Two passes: indirect /Length values can point at objects that appear
    after the stream, so all dictionaries must be known before slicing.
    """
    found = []
    for m in OBJ_RE.finditer(raw):
        num = int(m.group(1))
        dict_bytes, data_start = _find_stream(raw, m.end())
        found.append((num, dict_bytes, data_start))
    objs = {num: {"dict": d, "stream": None, "data_start": ds} for num, d, ds in found}
    for num, dict_bytes, data_start in found:
        if data_start is None:
            continue
        n = _length(dict_bytes, objs)
        if n is None or data_start + n > len(raw):
            continue
        objs[num]["stream"] = raw[data_start:data_start + n]
    return objs


def decompress(header, data):
    if data is None:
        return None
    if b"/FlateDecode" not in header:
        return data
    try:
        return zlib.decompress(data)
    except zlib.error:
        try:
            return zlib.decompressobj().decompress(data)
        except zlib.error:
            return None


def load_object_streams(objs):
    """Merge compressed objects from /Type /ObjStm into the object map."""
    extra = {}
    for num, obj in list(objs.items()):
        header = obj["dict"]
        if b"/ObjStm" not in header:
            continue
        data = decompress(header, obj["stream"])
        if not data:
            continue
        n = re.search(rb"/N\s+(\d+)", header)
        first = re.search(rb"/First\s+(\d+)", header)
        if not (n and first):
            continue
        count = int(n.group(1))
        first = int(first.group(1))
        head = data[:first].split()
        if len(head) < 2 * count:
            continue
        pairs = [(int(head[2 * i]), int(head[2 * i + 1])) for i in range(count)]
        for i, (objnum, off) in enumerate(pairs):
            end = first + (pairs[i + 1][1] if i + 1 < count else len(data) - first)
            extra[objnum] = {"dict": data[first + off:end], "stream": None, "compressed": True}
    objs.update(extra)
    return objs


def resolve_streams(raw, objs):
    """Resolve indirect /Length values that only became available after ObjStm merge."""
    changed = 0
    for num, obj in objs.items():
        if obj.get("stream") is not None or obj.get("data_start") is None:
            continue
        n = _length(obj["dict"], objs)
        if n is None or obj["data_start"] + n > len(raw):
            continue
        obj["stream"] = raw[obj["data_start"]:obj["data_start"] + n]
        changed += 1
    return changed


def stream_dict(header):
    return header


def all_payloads(objs):
    """Yield (objnum, header, decompressed_stream) for every readable stream."""
    for num, obj in objs.items():
        data = decompress(obj["dict"], obj.get("stream"))
        if data is not None:
            yield num, obj["dict"], data


def profile_info(data):
    if len(data) < 132 or data[36:40] != b"acsp":
        return None
    space = data[16:20].decode("latin1", "replace").strip()
    pcs = data[20:24].decode("latin1", "replace").strip()
    prof_id = data[84:100].hex()
    desc = None
    try:
        count = struct.unpack(">I", data[128:132])[0]
        for i in range(count):
            sig, off, size = struct.unpack(">4sII", data[132 + 12 * i:144 + 12 * i])
            if sig != b"desc":
                continue
            tag = data[off:off + size]
            if tag[:4] == b"mluc":
                n = struct.unpack(">I", tag[8:12])[0]
                vals = []
                for j in range(n):
                    length, pos = struct.unpack(">II", tag[20 + 12 * j:28 + 12 * j])
                    vals.append(tag[pos:pos + length].decode("utf-16-be", "replace"))
                desc = vals[0] if vals else None
            elif tag[:4] == b"desc":
                n = struct.unpack(">I", tag[8:12])[0]
                desc = tag[12:12 + max(0, n - 1)].decode("latin1", "replace")
            break
    except Exception:
        pass
    return {
        "space": space,
        "pcs": pcs,
        "id": prof_id,
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "description": desc,
        "isRGB": space.upper().startswith("RGB"),
        "isCMYK": space.upper().startswith("CMYK"),
    }


def extract_icc_profiles(raw, objs):
    profiles = []
    refs = set()
    for pat in (rb"/ICCBased\s+(\d+)\s+(\d+)\s+R",
                rb"/DefaultCMYK\s+(\d+)\s+(\d+)\s+R"):
        refs.update(int(m.group(1)) for m in re.finditer(pat, raw))
    for ref in sorted(refs):
        obj = objs.get(ref)
        if not obj:
            continue
        data = decompress(obj["dict"], obj.get("stream"))
        info = profile_info(data) if data else None
        if not info:
            continue
        n = re.search(rb"/N\s+(\d+)", obj["dict"])
        alt = re.search(rb"/Alternate\s*/(\w+)", obj["dict"])
        info.update({
            "object": ref,
            "n": int(n.group(1)) if n else None,
            "alternate": alt.group(1).decode() if alt else None,
            "data": data,
        })
        profiles.append(info)
    return profiles


def profile_by_ref(objs, ref):
    obj = objs.get(ref)
    if not obj:
        return None
    data = decompress(obj["dict"], obj.get("stream"))
    info = profile_info(data) if data else None
    if info:
        info["object"] = ref
        info["data"] = data
    return info


def _num_list(raw):
    return [float(x) for x in re.findall(rb"-?\d+(?:\.\d+)?", raw)]


def page_boxes(raw, objs):
    boxes = {}
    for num, obj in objs.items():
        d = obj["dict"]
        if not re.search(rb"/Type\s*/Page\b", d):
            continue
        for key in (b"MediaBox", b"CropBox", b"TrimBox", b"BleedBox", b"ArtBox"):
            m = re.search(rb"/" + key + rb"\s*\[([^\]]*)\]", d)
            if m:
                boxes[key.decode()] = _num_list(m.group(1))
        break
    if "MediaBox" not in boxes:
        m = re.search(rb"/MediaBox\s*\[([^\]]*)\]", raw)
        if m:
            boxes["MediaBox"] = _num_list(m.group(1))
    return boxes


def count_structure(objs):
    header_blob = b"\n".join(obj["dict"] for obj in objs.values())
    streams = []
    for _, header, data in all_payloads(objs):
        if b"/Subtype /Image" in header or b"/Subtype/Image" in header:
            continue
        # ObjStm payloads are already merged into individual objects; scanning
        # both would double-count /Type3 and /SMask.
        if b"/ObjStm" in header:
            continue
        streams.append(data)
    blob = header_blob + b"\n" + b"\n".join(streams)
    sep_names = [x.decode("latin1", "replace") for x in re.findall(rb"/Separation\s*/([^\s/\]>]+)", blob)]
    return {
        "type3": len(re.findall(rb"/Subtype\s*/Type3", blob)),
        "baseFont": len(re.findall(rb"/BaseFont", blob)),
        "images": len(re.findall(rb"/Subtype\s*/Image", blob)),
        "smask": len(re.findall(rb"/SMask", blob)),
        "separation": len(re.findall(rb"/Separation", blob)),
        "separationNames": sorted(set(sep_names)),
        "allSeparation": any(x.lower() == "all" for x in sep_names),
        "deviceN": len(re.findall(rb"/DeviceN", blob)),
        "overprintTrue": len(re.findall(rb"/OP\s+true", blob)) + len(re.findall(rb"/op\s+true", blob)),
        "overprintMode": len(re.findall(rb"/OPM\s*\d", blob)),
        "outputIntent": len(re.findall(rb"/OutputIntent", blob)),
        "deviceRGB": len(re.findall(rb"/DeviceRGB", blob)),
        "deviceCMYK": len(re.findall(rb"/DeviceCMYK", blob)),
        "iccbased": len(re.findall(rb"/ICCBased", blob)),
        "objStm": len(re.findall(rb"/Type\s*/ObjStm", blob)),
        "indexed": len(re.findall(rb"/Indexed", blob)),
        "opmTrue": len(re.findall(rb"/OPM\s*1", blob)),
    }


def scan_colours(objs):
    """Extract colour operators from content streams.

    Returns a list of dicts: space (rgb/cmyk/gray), values (0..1),
    count, hex, cmyk.
    """
    counts = {}
    for _, header, data in all_payloads(objs):
        if b"/Subtype /Image" in header or b"/Subtype/Image" in header:
            continue
        if not any(op in data for op in (b" rg", b" RG", b" k", b" K", b" scn", b" SCN", b" sc", b" SC")):
            continue
        num = rb"(-?(?:\d+(?:\.\d*)?|\.\d+))"
        patterns = [
            (re.compile(num + rb"\s+" + num + rb"\s+" + num + rb"\s+(rg|RG)\b"), "rgb", 3),
            (re.compile(num + rb"\s+" + num + rb"\s+" + num + rb"\s+" + num + rb"\s+(k|K)\b"), "cmyk", 4),
            (re.compile(num + rb"\s+(g|G)\b"), "gray", 1),
        ]
        for rx, space, n in patterns:
            for m in rx.finditer(data):
                vals = tuple(max(0.0, min(1.0, float(m.group(i)))) for i in range(1, n + 1))
                key = (space, vals)
                counts[key] = counts.get(key, 0) + 1
        # scn/sc may carry 3 (RGB) or 4 (CMYK) components. Match the optional
        # fourth number in one pattern so a 4-component operator is not also
        # counted as a 3-component RGB operator.
        scn = re.compile(num + rb"\s+" + num + rb"\s+" + num + rb"\s+(?:" + num + rb"\s+)?(sc|SC|scn|SCN)\b")
        for m in scn.finditer(data):
            if m.group(4) is not None:
                vals = tuple(max(0.0, min(1.0, float(m.group(i)))) for i in range(1, 5))
                key = ("cmyk", vals)
            else:
                vals = tuple(max(0.0, min(1.0, float(m.group(i)))) for i in range(1, 4))
                key = ("rgb", vals)
            counts[key] = counts.get(key, 0) + 1
    out = []
    for (space, vals), count in counts.items():
        if space == "rgb":
            rgb = tuple(int(round(v * 255)) for v in vals)
            hexv = "#%02X%02X%02X" % rgb
            cmyk = None
        elif space == "cmyk":
            rgb = None
            cmyk = tuple(round(v * 100, 2) for v in vals)
            hexv = None
        else:
            rgb = None
            cmyk = None
            hexv = None
        out.append({"space": space, "values": [round(v, 5) for v in vals],
                    "rgb": rgb, "hex": hexv, "cmyk": cmyk, "count": count})
    out.sort(key=lambda x: -x["count"])
    return out


def type3_painting_glyphs(path):
    """Count Type3 CharProc streams that contain a painting operator."""
    raw = open(path, "rb").read()
    objs = load_object_streams(load_objects(raw))
    resolve_streams(raw, objs)
    painting = 0
    for obj in objs.values():
        fd = obj["dict"]
        if b"/Subtype /Type3" not in fd and b"/Subtype/Type3" not in fd:
            continue
        cp = re.search(rb"/CharProcs\s*<<(.*?)>>", fd, re.S)
        if cp:
            blob = cp.group(1)
        else:
            ref = re.search(rb"/CharProcs\s+(\d+)\s+0\s+R", fd)
            blob = (objs.get(int(ref.group(1))) or {}).get("dict", b"") if ref else b""
        for m in re.finditer(rb"/(\w+)\s+(\d+)\s+0\s+R", blob):
            co = objs.get(int(m.group(2)))
            if not co:
                continue
            data = decompress(co["dict"], co.get("stream"))
            if data and re.search(rb"(^|\s)(f|F|f\*|F\*|S|s|B|B\*|b|b\*|sh|Do)\s", data):
                painting += 1
    return painting


def colour_contexts(path, cmyk, window=320):
    """Find where a CMYK operator occurs and whether it is text or a form/mask."""
    target = tuple(round(float(x), 2) for x in cmyk)
    raw = open(path, "rb").read()
    objs = load_object_streams(load_objects(raw))
    resolve_streams(raw, objs)
    out = []
    num = rb"(-?(?:\d+(?:\.\d*)?|\.\d+))"
    rx = re.compile(num + rb"\s+" + num + rb"\s+" + num + rb"\s+" + num + rb"\s+(k|K)\b")
    for n, header, data in all_payloads(objs):
        if b"/Subtype /Image" in header or b"/Subtype/Image" in header:
            continue
        for m in rx.finditer(data):
            vals = tuple(round(float(m.group(i)) * 100.0, 2) for i in range(1, 5))
            if vals != target:
                continue
            after = data[m.end():m.end() + window]
            before = data[max(0, m.start() - 160):m.start()]
            kind = "text" if (b"BT" in after and (b"Tj" in after or b"TJ" in after)) else "other"
            mask = (b"/Subtype/Form" in header and
                    (b"/Group" in header or b"/SMask" in header))
            out.append({"object": n, "kind": kind, "mask": mask,
                        "before": before.decode("latin1", "replace").replace("\n", " | "),
                        "after": after[:200].decode("latin1", "replace").replace("\n", " | ")})
    return out


def vector_max_tac(objs):
    best = None
    for c in scan_colours(objs):
        if c["space"] == "cmyk" and c["cmyk"] is not None:
            tac = sum(c["cmyk"])
            if best is None or tac > best["tac"]:
                best = {"tac": round(tac, 2), "cmyk": c["cmyk"], "count": c["count"]}
    return best


def _cs_kind(objs, blob):
    blob = blob or b""
    if b"/CalRGB" in blob or b"/DeviceRGB" in blob:
        return "rgb"
    if b"/DeviceCMYK" in blob:
        return "cmyk"
    if b"/DeviceGray" in blob:
        return "gray"
    m = re.search(rb"/ICCBased\s+(\d+)\s+0\s+R", blob)
    if m:
        prof = profile_by_ref(objs, int(m.group(1)))
        if prof:
            return "rgb" if prof.get("isRGB") else "cmyk" if prof.get("isCMYK") else "other"
    # Indexed base is the token right after /Indexed.
    m = re.search(rb"/Indexed\s*(\[[^\]]*\]|/[A-Za-z0-9]+|\d+\s+0\s+R)", blob)
    if m:
        base = m.group(1)
        if base.endswith(b" 0 R"):
            ref = int(re.search(rb"(\d+)", base).group(1))
            base = (objs.get(ref) or {}).get("dict", base)
        return _cs_kind(objs, base)
    return "unknown"


def output_intents(path):
    """Follow Catalog -> /OutputIntents -> /DestOutputProfile."""
    raw = open(path, "rb").read()
    objs = load_object_streams(load_objects(raw))
    resolve_streams(raw, objs)
    catalog = None
    for obj in objs.values():
        d = obj["dict"]
        if b"/Type /Catalog" in d or b"/Type/Catalog" in d:
            catalog = d
            break
    if not catalog:
        return []
    m = re.search(rb"/OutputIntents\s*(\[(.*?)\]|(\d+)\s+0\s+R)", catalog, re.S)
    if not m:
        return []
    blob = m.group(1)
    if m.group(3):
        ref = int(m.group(3))
        blob = (objs.get(ref) or {}).get("dict", b"")
    out = []
    for dm in re.finditer(rb"<<(.*?)>>", blob, re.S):
        d = dm.group(1)
        dest = re.search(rb"/DestOutputProfile\s+(\d+)\s+0\s+R", d)
        cond = re.search(rb"/OutputConditionIdentifier\s*\(([^)]*)\)", d)
        prof = profile_by_ref(objs, int(dest.group(1))) if dest else None
        out.append({
            "condition": cond.group(1).decode("latin1", "replace") if cond else None,
            "destProfile": ({k: prof[k] for k in ("description", "space", "sha256", "isRGB", "isCMYK")}
                            if prof else None),
        })
    return out


def image_colours(path):
    raw = open(path, "rb").read()
    objs = load_object_streams(load_objects(raw))
    resolve_streams(raw, objs)
    out = []
    for num, obj in objs.items():
        d = obj["dict"]
        if b"/Subtype /Image" not in d and b"/Subtype/Image" not in d:
            continue
        m = re.search(rb"/ColorSpace\s*(\[[^\]]*\]|/[A-Za-z0-9]+|\d+\s+0\s+R)", d)
        blob = m.group(1) if m else b""
        if blob.endswith(b" 0 R"):
            ref = int(re.search(rb"(\d+)", blob).group(1))
            blob = (objs.get(ref) or {}).get("dict", blob)
        out.append({"object": num, "kind": _cs_kind(objs, blob),
                    "colorSpace": blob[:120].decode("latin1", "replace")})
    return out


def thumbnails(path):
    raw = open(path, "rb").read()
    objs = load_object_streams(load_objects(raw))
    resolve_streams(raw, objs)
    out = []
    for m in re.finditer(rb"/Thumb\s+(\d+)\s+0\s+R", raw):
        ref = int(m.group(1))
        obj = objs.get(ref)
        if not obj:
            continue
        d = obj["dict"]
        w = re.search(rb"/Width\s+(\d+)", d)
        h = re.search(rb"/Height\s+(\d+)", d)
        cs = re.search(rb"/ColorSpace\s+(\d+)\s+0\s+R", d)
        cs_dict = b""
        if cs:
            cs_dict = (objs.get(int(cs.group(1))) or {}).get("dict", b"")
        out.append({
            "object": ref,
            "width": int(w.group(1)) if w else None,
            "height": int(h.group(1)) if h else None,
            "deviceRGB": b"/DeviceRGB" in d or b"/DeviceRGB" in cs_dict,
            "indexed": b"/Indexed" in d or b"/Indexed" in cs_dict,
            "dict": d[:300].decode("latin1", "replace"),
        })
    return out


def inspect(path):
    raw = open(path, "rb").read()
    objs = load_object_streams(load_objects(raw))
    resolve_streams(raw, objs)
    profiles = extract_icc_profiles(raw, objs)
    boxes = page_boxes(raw, objs)
    return {
        "path": path,
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "header": raw[:9].decode("latin1", "replace").strip(),
        "pages": len(re.findall(rb"/Type\s*/Page[^s]", raw)),
        "boxes": boxes,
        "profiles": [{k: v for k, v in p.items() if k != "data"} for p in profiles],
        "structure": count_structure(objs),
        "colours": scan_colours(objs),
        "vectorMaxTac": vector_max_tac(objs),
    }
