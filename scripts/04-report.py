#!/usr/bin/env python3
"""04-report.py <job-id>

Merge all evidence into qa.json / qa.md / checklist.md.

Status vocabulary:
  pass    = verified with evidence
  fail    = a required condition is violated
  pending = not verified, or depends on the printer / a human GUI step
  warn    = non-blocking observation with evidence

The report never certifies print colour, PDF/X compliance or contract proof.
"""

import hashlib
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "lib"))
import pdfprobe  # noqa: E402
import acceptance  # noqa: E402
import source  # noqa: E402


def load(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def sha(path):
    if not path or not os.path.exists(path):
        return None
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def check(cid, label, status, value, expected, evidence=""):
    return {"id": cid, "label": label, "status": status,
            "value": value, "expected": expected, "evidence": evidence}


def main():
    if len(sys.argv) != 2:
        print("usage: 04-report.py <job-id>", file=sys.stderr)
        return 64
    job_id = sys.argv[1]
    job_dir = os.path.join(ROOT, "jobs", job_id)
    if not os.path.isdir(job_dir):
        print("job dir not found: %s" % job_dir, file=sys.stderr)
        return 66

    spec = load(os.path.join(ROOT, "config", "print-spec.json"))
    job = load(os.path.join(job_dir, "job.json"))
    prep = load(os.path.join(job_dir, "prepare.json"))
    tw, th = None, None
    ai_res = load(os.path.join(job_dir, "work", "qa-result.json"))
    if not ai_res or ai_res.get("script") != "03-qa":
        ai_res = load(os.path.join(job_dir, "result.json"))
    pf = load(os.path.join(job_dir, "preflight.json"))
    ca = load(os.path.join(job_dir, "color-audit.json"))
    proof = load(os.path.join(job_dir, "proof.json"))
    rgb_audit = load(os.path.join(job_dir, "work", "rgb-audit.json"))
    src_audit = load(os.path.join(job_dir, "work", "source-audit.json"))
    gate3 = load(os.path.join(job_dir, "gate3.json"))

    checks = []

    def missing(name, why=""):
        checks.append(check("evidence.%s" % name, "missing evidence %s" % name, "pending",
                            "missing", "present", why))

    for name, obj in (("prepare.json", prep), ("result.json", ai_res),
                      ("preflight.json", pf), ("color-audit.json", ca),
                      ("proof.json", proof), ("work/source-audit.json", src_audit),
                      ("work/rgb-audit.json", rgb_audit)):
        if obj is None:
            missing(name)

    # ---------- hashes ----------
    src_rel = (job.get("source") or {}).get("pdf")
    src_path = os.path.join(job_dir, src_rel) if src_rel else None
    cmyk_path = os.path.join(job_dir, prep["output"]) if prep and prep.get("output") else None
    ai_rel = (job.get("output") or {}).get("ai")
    ai_path = os.path.join(job_dir, ai_rel) if ai_rel else None

    src_hash = sha(src_path)
    cmyk_hash = sha(cmyk_path)
    ai_hash = sha(ai_path)

    baseline = job.get("baselineJob")
    baseline_src = None
    if baseline and src_rel:
        baseline_src = os.path.join(ROOT, "jobs", baseline, src_rel)
    expected_src = (job.get("source") or {}).get("sha256") or (prep or {}).get("inputSha256")
    src_ok = bool(src_hash) and (not expected_src or src_hash == expected_src)
    if baseline_src and os.path.exists(baseline_src):
        src_ok = src_ok and src_hash == sha(baseline_src)
    checks.append(check("evidence.source", "RGB source not modified",
                        "pass" if src_ok else "fail", src_hash, expected_src or "matches baseline",
                        "baseline: %s" % baseline if baseline else ""))

    prep_ok = bool(cmyk_hash) and (not prep or not prep.get("outputSha256") or cmyk_hash == prep["outputSha256"])
    checks.append(check("evidence.cmyk", "CMYK intermediate hash",
                        "pass" if prep_ok else "fail", cmyk_hash,
                        (prep or {}).get("outputSha256"), ""))

    ai_ok = bool(ai_hash) and (not ai_res or not ai_res.get("deliverableSha256")
                               or ai_hash == ai_res.get("deliverableSha256"))
    checks.append(check("evidence.ai", "deliverable AI hash matches QA time",
                        "pass" if ai_ok else "fail", ai_hash,
                        (ai_res or {}).get("deliverableSha256"),
                        "a same-size swap still shows in the hash"))

    # Evidence files must refer to the same inputs as the deliverable.
    if pf:
        pf_ok = pf.get("sourcePdfSha256") == cmyk_hash
        checks.append(check("evidence.preflight", "preflight binds current cmyk.pdf",
                            "pass" if pf_ok else "fail",
                            pf.get("sourcePdfSha256"), cmyk_hash,
                            "rerun 05 after 01"))
    if proof:
        ph = proof.get("hashes") or {}
        pr_ok = (ph.get("source") == src_hash
                 and ph.get("cmyk") == cmyk_hash
                 and ph.get("ai") == ai_hash)
        checks.append(check("evidence.proof", "proof binds current cmyk.pdf / AI",
                            "pass" if pr_ok else "fail",
                            proof.get("hashes"), {"cmyk": cmyk_hash, "ai": ai_hash},
                            "rerun 07 after 02/03"))
    if ca:
        brand_sha = sha(os.path.join(ROOT, "color", "brand-map.json"))
        ca_ok = (ca.get("source", {}).get("sha256") == src_hash
                 and ca.get("cmykSha256") == cmyk_hash
                 and ca.get("brandMapSha256") == brand_sha)
        checks.append(check("evidence.colorAudit", "colour map binds current source / cmyk.pdf",
                            "pass" if ca_ok else "fail",
                            {"source": ca.get("source", {}).get("sha256"),
                             "cmyk": ca.get("cmykSha256")},
                            {"source": src_hash, "cmyk": cmyk_hash},
                            "rerun 06 after 01"))

    final_pdf = os.path.join(job_dir, job["output"]["pdf"]) if job.get("output", {}).get("pdf") else None
    final_hash = sha(final_pdf)
    if final_hash:
        g3_ok = (bool(gate3) and gate3.get("sha256") == final_hash
                 and gate3.get("aiSha256") == ai_hash
                 and gate3.get("acceptanceHash") == acceptance.digest(job, spec))
        checks.append(check("evidence.gate3", "Gate 3 binds final PDF",
                            "pass" if g3_ok else "fail",
                            (gate3 or {}).get("sha256"), final_hash,
                            "rerun 08 after the final PDF is replaced"))
    # No final PDF: gate3.finalPdf already carries the PENDING state.

    cfg_hash = sha(os.path.join(ROOT, "config", "print-spec.json"))
    checks.append(check("evidence.config", "print config hash",
                        "pass" if prep and prep.get("configSha256") == cfg_hash else "pending",
                        cfg_hash, (prep or {}).get("configSha256"),
                        "old jobs carry no configSha256; binding appears after rerunning 01"))

    target_path = ((prep or {}).get("conversion") or {}).get("targetICC")
    target_hash = sha(target_path) if target_path else None
    checks.append(check("evidence.targetICC", "target ICC not replaced",
                        "pass" if target_hash and target_hash == (prep or {}).get("iccSha256") else "fail",
                        target_hash, (prep or {}).get("iccSha256"), target_path or ""))

    # ---------- conversion ----------
    if prep:
        checks.append(check("prep.color", "colour space after conversion",
                            "pass" if prep.get("deviceRGB", 1) == 0 and prep.get("deviceCMYK", 0) > 0 else "fail",
                            "DeviceRGB×%s / DeviceCMYK×%s" % (prep.get("deviceRGB"), prep.get("deviceCMYK")),
                            "DeviceRGB 0, DeviceCMYK > 0", "ICC: %s" % prep.get("icc")))
        checks.append(check("prep.pages", "page count after conversion",
                            "pass" if prep.get("pages") == 1 else "fail", prep.get("pages"), 1, ""))
        src_info = prep.get("sourceICC") or {}
        checks.append(check("prep.sourceICC", "source ICC",
                            "pass" if prep.get("sourceKind") == "rgb-icc" and src_info.get("isRGB") else "fail",
                            {"kind": prep.get("sourceKind"), "description": src_info.get("description"),
                             "space": src_info.get("space"), "sha256": prep.get("sourceICCsha256")},
                            "embedded RGB ICC with recorded hash", ""))
        conv = prep.get("conversion") or {}
        conv_ok = (conv.get("renderingIntent") == 1
                   and conv.get("blackPointCompensation") is True
                   and conv.get("overrideICC") is False
                   and conv.get("sourceICCsha256") == prep.get("sourceICCsha256")
                   and conv.get("targetICCsha256") == prep.get("iccSha256"))
        checks.append(check("prep.conversion", "conversion settings match the record",
                            "pass" if conv_ok else "fail",
                            {k: conv.get(k) for k in ("renderingIntentName", "blackPointCompensation",
                                                      "overrideICC", "gsVersion")},
                            "Relative intent 1, BPC true, OverrideICC false", ""))
        # Page size
        tw, th = None, None
        if job.get("trim", {}).get("preset"):
            tw, th = spec["trimPresetsMm"][job["trim"]["preset"]]
        elif job.get("trim", {}).get("w"):
            tw, th = job["trim"]["w"], job["trim"]["h"]
        if tw:
            b = float(job.get("bleedMm", spec["print"].get("bleedMm", 0)) or 0)
            page = prep.get("pageMm") or [0, 0]
            factor = 1.0 if source.is_direct_pdf(job) else (1.0 / float(spec.get("figma", {}).get("pxToPt", 0.75)))
            exp_w = (tw + 2 * b) * factor
            exp_h = (th + 2 * b) * factor
            ok = (abs(page[0] - exp_w) <= 0.5 and abs(page[1] - exp_h) <= 0.5) or \
                 (abs(page[0] - exp_h) <= 0.5 and abs(page[1] - exp_w) <= 0.5)
            checks.append(check("prep.pageSize", "intermediate page size",
                                "pass" if ok else "fail", page,
                                "about %.1f × %.1f mm" % (exp_w, exp_h),
                                "includes %.1f mm bleed; %s" % (
                                    b, "direct sRGB PDF is 1:1" if source.is_direct_pdf(job)
                                    else "Figma export uses pxToPt=%.3f" % (1.0 / factor))))
    else:
        checks.append(check("prep.color", "conversion check", "fail", "missing prepare.json", "present", ""))

    # ---------- Illustrator result ----------
    if ai_res and ai_res.get("ok"):
        checks.append(check("ai.colorMode", "document colour mode",
                            "pass" if ai_res.get("isCMYK") else "fail",
                            ai_res.get("docColorSpace"), "CMYK", ""))
        ab = ai_res.get("artboardMm") or {}
        if job.get("trim", {}).get("preset"):
            tw, th = spec["trimPresetsMm"][job["trim"]["preset"]]
        else:
            tw, th = job["trim"]["w"], job["trim"]["h"]
        d = max(abs(ab.get("w", 0) - tw), abs(ab.get("h", 0) - th))
        checks.append(check("ai.artboard", "artboard size",
                            "pass" if d <= 0.2 else "fail",
                            "%s × %s mm" % (ab.get("w"), ab.get("h")),
                            "%s × %s mm" % (tw, th), "max deviation %.2f mm" % d))
        checks.append(check("ai.linked", "no linked items",
                            "pass" if ai_res.get("linkedItems", 1) == 0 else "fail",
                            ai_res.get("linkedItems"), 0, ""))
        checks.append(check("ai.linkedRasters", "externally linked rasters",
                            "pass" if not ai_res.get("linkedRasters") else "fail",
                            ai_res.get("linkedRasters") or 0, 0, ""))
        usage = ai_res.get("colorUsage") or {}
        rgb = usage.get("rgb", 0)
        other = usage.get("other", 0)
        errs = ai_res.get("scanErrors", 0)
        checks.append(check("ai.rgbResidue", "residual non-CMYK vector colour",
                            "pass" if rgb == 0 and other == 0 and errs == 0 else "fail",
                            "RGB %s / other %s / read failures %s" % (rgb, other, errs), 0,
                            "CMYK %s / spot %s / grey %s" % (usage.get("cmyk"), usage.get("spot"), usage.get("gray"))))
        rasters = ai_res.get("rasters") or []
        bad = [r for r in rasters if "CMYK" not in str(r.get("imageColorSpace", ""))]
        checks.append(check("ai.rasterColor", "raster internal colour space",
                            "pass" if not bad else "fail",
                            ", ".join(sorted({str(r.get("imageColorSpace")) for r in rasters})) or "no rasters",
                            "ImageColorSpace.CMYK", ""))
        spots = ai_res.get("spotColors") or []
        checks.append(check("ai.spotColors", "spot colour (excluding Registration)",
                            "pending" if spots else "pass", spots or 0, 0,
                            "spot colour needs print shop confirmation" if spots else ""))
        checks.append(check("ai.text", "editable text",
                            "pass" if ai_res.get("textFrames", 0) == 0 else "pending",
                            ai_res.get("textFrames", 0), 0,
                            "this flow outlines text; a non-zero value means the source behaviour changed"))
        types = ai_res.get("itemTypes") or {}
        checks.append(check("ai.nonNative", "non-native artwork",
                            "pass" if types.get("NonNativeItem", 0) == 0 else "pending",
                            types.get("NonNativeItem", 0), 0, ""))
        ov = ai_res.get("overflowMm") or {}
        b = float(job.get("bleedMm", spec["print"].get("bleedMm", 0)) or 0)
        worst = max((abs(v - b) for v in ov.values()), default=b)
        checks.append(check("ai.overflow", "bleed per side",
                            "pass" if worst <= 0.5 else "warn",
                            ov, "%.1f mm on each side (±0.5)" % b,
                            "max deviation %.2f mm" % worst))
        prof = ai_res.get("colorProfileName") or ""
        icc_name = spec["print"].get("iccName", "")
        profile_match = bool(prof) and bool(icc_name) and icc_name.upper() in prof.upper()
        checks.append(check("ai.colorProfile", "Illustrator document profile",
                            "pass" if profile_match else "pending",
                            prof or "not returned by ExtendScript", icc_name,
                            "28.4.1 exposes no reliable read; GUI check required"))
        # CMYK set: cmyk.pdf vs .ai PDF part
        if cmyk_path and ai_path and os.path.exists(cmyk_path) and os.path.exists(ai_path):
            try:
                ci = pdfprobe.inspect(cmyk_path)
                ai = pdfprobe.inspect(ai_path)
                cset = [tuple(round(x, 2) for x in c["cmyk"]) for c in ci["colours"] if c["space"] == "cmyk"]
                aset = [tuple(round(x, 2) for x in c["cmyk"]) for c in ai["colours"] if c["space"] == "cmyk"]
                missing_c = []
                max_delta = 0.0
                for c in cset:
                    if not aset:
                        missing_c.append(c)
                        continue
                    best = min(sum(abs(a - b) for a, b in zip(c, x)) for x in aset)
                    max_delta = max(max_delta, best)
                    if best > 0.3:
                        missing_c.append(c)
                visible_ok = bool(proof and (proof.get("cmykVsAi") or {}).get("status") == "pass")

                def _non_painting(t):
                    ctx = pdfprobe.colour_contexts(cmyk_path, t)
                    vel = (pf or {}).get("vectorMaxTac") or {}
                    same = tuple(round(x, 2) for x in t) == tuple(round(x, 2) for x in (vel.get("cmyk") or []))
                    visible_zero = vel.get("visibleAt300") is False if same else False
                    all_nonink = bool(ctx) and all(
                        (c.get("kind") == "text" or c.get("mask")) for c in ctx)
                    if any(c.get("kind") == "text" for c in ctx):
                        all_nonink = all_nonink and pdfprobe.type3_painting_glyphs(cmyk_path) == 0
                    return all_nonink and visible_zero, ctx

                missing_evidence = []
                missing_nonpainting = True
                for t in missing_c:
                    ok, ctx = _non_painting(t)
                    missing_nonpainting = missing_nonpainting and ok
                    missing_evidence.append({"cmyk": list(t), "nonPainting": ok,
                                             "contexts": [{k: c[k] for k in ("object", "kind", "mask")}
                                                          for c in ctx]})
                if not missing_c:
                    cs_status, cs_note = "pass", "parsed from the content stream, no thumbnail dependency"
                elif missing_nonpainting:
                    cs_status, cs_note = "pass", (
                        "missing tuples appear only in invisible Type3 text or transparency mask contexts; "
                        "visible pixels at 300 dpi are 0")
                elif visible_ok:
                    cs_status, cs_note = "pending", (
                        "missing tuples show no difference in flat colour rendering, but a flat mask does not prove occlusion; "
                        "confirm each item in AI before sending to print")
                else:
                    cs_status, cs_note = "fail", "visible rendering differs too; not plain occlusion"
                checks.append(check("ai.cmykSet", "AI content stream keeps CMYK values",
                                    cs_status,
                                    {"cmykPdf": len(cset), "ai": len(aset), "maxChannelDelta": round(max_delta, 2),
                                     "missing": missing_c[:5], "missingEvidence": missing_evidence,
                                     "visibleProof": visible_ok},
                                    "cmyk.pdf and .ai CMYK tuples match (±0.3)", cs_note))
                thumbs = pdfprobe.thumbnails(ai_path)
                page_rgb = sum(c["count"] for c in ai["colours"] if c["space"] == "rgb")
                thumb_rgb = any(t.get("deviceRGB") or t.get("indexed") for t in thumbs)
                if page_rgb == 0 and (thumb_rgb or
                                      (ai["structure"].get("deviceRGB", 0) == 0
                                       and ai["structure"].get("indexed", 0) == 0)):
                    fs_status = "pass"
                    fs_note = "RGB/Indexed only in /Thumb, page content has no RGB"
                else:
                    fs_status = "pending"
                    fs_note = "RGB reference not in an identified /Thumb; locate it by hand"
                cc = Counter()
                ac = Counter()
                for c in ci["colours"]:
                    if c["space"] == "cmyk":
                        cc[tuple(round(x, 2) for x in c["cmyk"])] += c.get("count", 0)
                for c in ai["colours"]:
                    if c["space"] == "cmyk":
                        ac[tuple(round(x, 2) for x in c["cmyk"])] += c.get("count", 0)
                short = [{"cmyk": list(k), "cmykPdf": n, "ai": ac.get(k, 0)}
                         for k, n in cc.items() if ac.get(k, 0) < n]
                short_open = []
                for item in short:
                    ok, _ = _non_painting(tuple(item["cmyk"]))
                    if not ok:
                        short_open.append(item)
                checks.append(check("ai.cmykCounts", "AI object count per CMYK tuple",
                                    "pass" if not short_open else "pending",
                                    {"short": short_open[:6], "checked": short[:6],
                                     "cmykTuples": len(cc), "aiTuples": len(ac)},
                                    "each tuple has at least as many operators in AI as in cmyk.pdf",
                                    "one missing same-colour object can be a lost line or glyph; "
                                    "only tuples proven to be invisible text or mask do not block"))

                checks.append(check("ai.fileColorspace", ".ai RGB/Indexed reference attribution",
                                    fs_status,
                                    {"deviceRGB": ai["structure"].get("deviceRGB", 0),
                                     "indexed": ai["structure"].get("indexed", 0),
                                     "pageRGBOperators": page_rgb,
                                     "thumbnails": thumbs},
                                    "page content has 0 RGB; thumbnails listed separately", fs_note))
            except Exception as exc:  # noqa: BLE001
                checks.append(check("ai.cmykSet", "AI content stream keeps CMYK values", "pending",
                                    str(exc), "parseable", ""))
        # Stroke / dark policy
        checks.append(check("ai.strokeWidth", "thinnest stroke",
                            "pending" if ai_res.get("strokeCount", 0) == 0 else
                            ("pass" if (ai_res.get("strokeMinMm") or 0) >= 0.1 else "pending"),
                            {"strokes": ai_res.get("strokeCount"),
                             "minMm": ai_res.get("strokeMinMm"),
                             "under0_1": ai_res.get("strokeUnder0_1Mm")},
                            "≥ 0.1 mm; outlined text needs a separate visual check",
                            "stroked=0 here, so this does not prove outlined text hairlines are safe"))
        nd = ai_res.get("neutralDark", 0)
        pk = ai_res.get("pureK", 0)
        pn = ((pf or {}).get("neutral") or {})
        four = pn.get("fourColourGreyPercent", 0) or 0
        pn_pure = pn.get("pureKPercent", 0) or 0
        needs_policy = (nd > 0 or pk > 0 or four > 0
                        or (pn.get("neutralDarkPixels", 0) > 0 and pn_pure == 0))
        checks.append(check("ai.blackPolicy", "black plate / four-colour grey",
                            "pending" if needs_policy else "pass",
                            {"aiNeutralDark": nd, "aiPureK": pk,
                             "preflightFourColourGreyPercent": four,
                             "preflightPureKPercent": pn_pure,
                             "histogram": dict(sorted((ai_res.get("cmykHistogram") or {}).items(),
                                                      key=lambda kv: -kv[1])[:5])},
                            "confirm with the print shop black plate / trapping policy; do not change brand darks on your own",
                            "body text is four-colour grey/black, pure K is 0" if needs_policy else ""))
    elif ai_res is not None:
        checks.append(check("ai.result", "Illustrator self-check result", "fail",
                            ai_res.get("error", "unknown"), "ok:true", ""))

    # ---------- preflight ----------
    if pf and pf.get("ok"):
        pb = pf.get("bleed") or {}
        band_ok = all(v.get("whitePixels", 1) == 0 for v in (pb.get("sides") or {}).values())
        band_mm = (pb.get("bandMmActual") or {})
        band_ok = band_ok and band_mm.get("x", 0) >= 3.0 and band_mm.get("y", 0) >= 3.0
        checks.append(check("pf.bleed", "full 3 mm bleed band",
                            "pass" if band_ok else "fail",
                            {"sides": pb.get("sides"), "bandMmActual": band_mm,
                             "whitePixels": pb.get("whitePixels")},
                            "full 3 mm on all sides, pure white pixels 0",
                            "converted in trim coordinates, no 18 px intermediate approximation"))
        pt = pf.get("tac") or {}
        vec_info = pf.get("vectorMaxTac") or {}
        checks.append(check("pf.tac", "total area coverage (TAC)",
                            pt.get("status", "pending"),
                            {"raster150": pt.get("maxTacPercent"),
                             "raster300": (pt.get("dpi300") or {}).get("maxTacPercent"),
                             "vector": pf.get("vectorMaxTac"),
                             "vectorVisible": vec_info.get("visibleAt300"),
                             "visibleMax": pt.get("visibleMaxTacPercent"),
                             "limit": pt.get("limitPercent")},
                            "≤ print shop limit; report only when unconfirmed",
                            pt.get("note", "")))
        vec_tac = pt.get("visibleVectorTac") or 0
        if pt.get("limitPercent") is None and max(pt.get("visibleMaxTacPercent") or 0, vec_tac) >= 300:
            checks.append(check("pf.tacRisk", "TAC above the common 300% limit",
                                "warn",
                                {"visibleMax": pt.get("visibleMaxTacPercent"),
                                 "raster150": pt.get("maxTacPercent"),
                                 "raster300": (pt.get("dpi300") or {}).get("maxTacPercent"),
                                 "vector": vec_info.get("tac"),
                                 "vectorVisible": vec_info.get("visibleAt300")},
                                "print shop confirms a 300% or 330% limit",
                                "a vector maximum not visible in the 300 dpi raster counts as occluded or cropped; the raster still sets the visible maximum"))
        st = pf.get("structure") or {}
        col = pf.get("colours") or {}
        struct_ok = col.get("rgbOperators", 0) == 0 and st.get("separation", 0) == 0 and not st.get("allSeparation")
        checks.append(check("pf.structure", "intermediate structure",
                            "pass" if struct_ok else "fail",
                            {"rgbOperators": col.get("rgbOperators"),
                             "cmykOperators": col.get("cmykOperators"),
                             "separation": st.get("separation"),
                             "allSeparation": st.get("allSeparation"),
                             "overprint": st.get("overprintTrue"),
                             "type3": st.get("type3"),
                             "smask": st.get("smask")},
                            "RGB 0; no /All spot plate; confirm overprint item by item",
                            "Type3/SMask are intermediate facts; the final PDF is checked separately"))
        checks.append(check("pf.overprint", "overprint / overprint mode",
                            "pending" if (st.get("overprintTrue", 0) or st.get("opmTrue", 0)) else "pass",
                            {"overprintTrue": st.get("overprintTrue"), "opmTrue": st.get("opmTrue")},
                            0, "when non-zero, confirm in Acrobat output preview"))
        n = pf.get("neutral") or {}
        checks.append(check("pf.neutral", "four-colour grey / pure K (raster)",
                            "pending" if n.get("fourColourGreyPercent", 0) > 0 and n.get("pureKPercent", 0) == 0 else "pass",
                            n, "decided by the print shop black plate policy",
                            "raster statistics only; object-level values are in vectorMaxTac"))
    elif pf is not None:
        checks.append(check("pf.result", "preflight run result", "fail", pf.get("error", "unknown"), "ok:true", ""))

    # ---------- colour audit / proof ----------
    if ca:
        rows = ca.get("rows") or []
        unnamed = [r["hex"] for r in rows if not r.get("name")]
        unmatched = [r["hex"] for r in rows if not r.get("matched", True)]
        checks.append(check("color.rows", "brand colour map coverage",
                            "pass" if rows and not unnamed and not unmatched else "pending",
                            {"colours": len(rows), "unnamed": unnamed[:8], "unmatched": unmatched[:8]},
                            "every measured flat colour has a name and real CMYK", ""))
        cmm = (ca.get("summary") or {}).get("maxCmmDifference")
        unmatched = (ca.get("summary") or {}).get("unmatched") or []
        checks.append(check("color.controlProbe", "real gs path matches the ICC prediction",
                            "pass" if (not unmatched and cmm is not None and cmm <= 1.0) else "pending",
                            {"maxCmmDifference": cmm, "unmatched": unmatched},
                            "≤ 1.0 ΔE00; every source colour matches a real CMYK",
                            "LittleCMS prediction vs real cmyk.pdf, not just echoed settings"))
        tol = ca.get("tolerance") or {}
        limit = tol.get("deltaE00")
        rows_ca = ca.get("rows") or []
        over_rows = ([r["hex"] for r in rows_ca
                      if r.get("deltaE00Predicted") is not None and r["deltaE00Predicted"] > limit]
                     if limit is not None else [])
        checks.append(check("color.tolerance", "brand colour tolerance",
                            "pending" if limit is None else
                            ("fail" if over_rows else "pass"),
                            {"limit": limit, "overLimit": over_rows,
                             "max": (ca.get("summary") or {}).get("maxDeltaE00")},
                            "ΔE00 tolerance confirmed by the print shop / brand",
                            tol.get("note", "")))
    if proof:
        s = proof.get("sourceVsCmyk") or {}
        ok = s.get("median", 99) <= 2.0 and s.get("over10Percent", 99) <= 1.0
        checks.append(check("proof.sourceVsCmyk", "source vs delivery ICC simulation",
                            "pass" if ok else "fail",
                            {k: s.get(k) for k in ("median", "p95", "p99", "over10Percent")},
                            "median ≤ 2, over10 ≤ 1%", "ICC simulation, no paper white or physical print"))
        a = proof.get("cmykVsAi") or {}
        checks.append(check("proof.cmykVsAi", "CMYK intermediate vs AI",
                            "pass" if a.get("status") == "pass" else
                            ("pending" if a.get("status") == "pending" else "fail"),
                            {k: a.get(k) for k in ("median", "p99", "over5Percent", "alignment")},
                            "median ≤ 0.5, over5 ≤ 0.05%", a.get("note", "")))
        checks.append(check("proof.illustrator", "Illustrator Proof Setup",
                            "pending", "no GUI evidence collected", "Proof Colors + %s setting screenshot or readback" % spec["print"].get("iccName"),
                            "the local scripting dictionary has no Proof Setup interface; lcms simulation cannot replace it"))

    # ---------- Gate 1 ----------
    g1 = []
    if rgb_audit and rgb_audit.get("ok"):
        g1.append(check("gate1.rgbImport", "RGB working copy import",
                        "pass" if rgb_audit.get("documentColorSpace") == "DocumentColorSpace.RGB"
                        and "SRGB" in str(rgb_audit.get("colorProfileName", "")).upper()
                        and rgb_audit.get("placedItems", 0) == 0 else "pending",
                        {"space": rgb_audit.get("documentColorSpace"),
                         "profile": rgb_audit.get("colorProfileName"),
                         "placedItems": rgb_audit.get("placedItems")},
                        "RGB / sRGB2014 / no external links", ""))
        if src_audit:
            src_page = src_audit.get("boxes", {}).get("MediaBox")
            if src_page:
                sw = round((src_page[2] - src_page[0]) * 25.4 / 72, 2)
                sh = round((src_page[3] - src_page[1]) * 25.4 / 72, 2)
                ab = rgb_audit.get("artboardMm") or {}
                dd = max(abs(ab.get("w", 0) - sw), abs(ab.get("h", 0) - sh))
                g1.append(check("gate1.artboard", "source page vs RGB copy size",
                                "pass" if dd <= 0.2 else "fail",
                                {"source": [sw, sh], "rgbCopy": ab}, "±0.2 mm",
                                "max deviation %.2f mm" % dd))
            src_hex = sorted({c["hex"] for c in src_audit.get("colours", []) if c.get("space") == "rgb"})
            rgb_hex = sorted(((rgb_audit.get("pathRgbHistogram") or rgb_audit.get("rgbHistogram") or {})).keys())
            missing_hex = sorted(set(src_hex) - set(rgb_hex))
            extra_hex = sorted(set(rgb_hex) - set(src_hex))
            colour_status = "pass" if not missing_hex and not extra_hex else (
                "pending" if missing_hex == ["#000000"] and not extra_hex else "fail")
            colour_note = ""
            if colour_status == "pending" and ca and ai_path and os.path.exists(ai_path):
                row = next((r for r in ca.get("rows", []) if r.get("hex") == "#000000"), None)
                if row:
                    target = tuple(row.get("actualCmyk") or [])
                    ai = pdfprobe.inspect(ai_path)
                    aset = [tuple(c["cmyk"]) for c in ai["colours"] if c["space"] == "cmyk"]
                    if any(sum(abs(a - b) for a, b in zip(target, x)) <= 0.3 for x in aset):
                        colour_status = "pass"
                        colour_note = ("ExtendScript did not enumerate pure black outlined text; the AI content stream holds the matching CMYK tuple %s"
                                       % (" / ".join("%.1f" % x for x in target)))
            if colour_status == "pending" and missing_hex == ["#000000"] and not extra_hex:
                ai_set = next((c for c in checks if c["id"] == "ai.cmykSet"), None)
                cv = ((proof or {}).get("cmykVsAi") or {}).get("status")
                if ai_set and ai_set.get("status") == "pass" and cv == "pass":
                    colour_status = "pass"
                    colour_note = ("pure black exists only in invisible Type3 shells; ai.cmykSet proved nonPainting, "
                                   "cmykVsAi passed; visible rendering shows no difference")
            g1.append(check("gate1.colours", "source palette matches the RGB copy",
                            colour_status,
                            {"source": len(src_hex), "copy": len(rgb_hex),
                             "missing": missing_hex, "extra": extra_hex,
                             "pathFilled": rgb_audit.get("pathFilled")},
                            "HEX sets match", colour_note))
        g1.append(check("gate1.export", "RGB proof render",
                        "pass" if rgb_audit.get("exported") else "pending",
                        rgb_audit.get("exported"), True, "; ".join(rgb_audit.get("warnings") or [])))
        sva = (proof or {}).get("sourceVsRgbAi")
        if sva:
            v_ok = sva.get("median", 99) <= 1.0 and sva.get("over5Percent", 99) <= 0.05
            g1.append(check("gate1.visual", "source PDF vs RGB working copy",
                            "pass" if v_ok else "fail",
                            {k: sva.get(k) for k in ("median", "p95", "max", "over5Percent")},
                            "median ≤ 1, over5 ≤ 0.05%",
                            "150 dpi flat area comparison; artboard and bleed checks cover thin line geometry"))
        else:
            g1.append(check("gate1.visual", "source PDF vs RGB working copy",
                            "pending", "proof missing", "generated by 07-proof", ""))
    else:
        g1.append(check("gate1.rgbImport", "RGB working copy import", "pending",
                        "missing work/rgb-audit.json", "present", ""))

    g1.append(check("gate1.sourceHash", "source hash", "pass" if src_ok else "fail",
                    src_hash, expected_src or "matches baseline", ""))
    if source.is_direct_pdf(job):
        g1.append(check("gate1.sourceMode", "source mode", "pass",
                        {"kind": "srgb-pdf", "figmaSnapshot": "not applicable"},
                        "an existing sRGB PDF at final size",
                        "source hash, embedded ICC and page geometry carry Gate 1"))
    else:
        fb = load(os.path.join(ROOT, "color", "figma-baseline.json"))
        ff = load(os.path.join(ROOT, "color", "figma-final.json"))
        node_id = str((job.get("figma") or {}).get("nodeId") or "")

        def node_signature(entry):
            if not entry:
                return None
            method = entry.get("hashMethod") or "legacy"
            value = entry.get("contentSha256") or entry.get("contentFnv")
            return (method, value) if value else None

        def find_node(nodes):
            for key, entry in (nodes or {}).items():
                if str((entry or {}).get("nodeId")) == node_id:
                    return key, entry
            return None, None

        if fb and ff:
            before_key, before = find_node(fb.get("nodes") or {})
            after_key, after = find_node(ff.get("nodes") or {})
            if before and after:
                bs = node_signature(before)
                as_ = node_signature(after)
                same = bs is not None and bs == as_
                g1.append(check("gate1.figma", "Figma node unchanged", "pass" if same else "fail",
                                {"nodeId": node_id, "baselineKey": before_key,
                                 "finalKey": after_key, "before": before, "after": after,
                                 "hashMethod": bs[0] if bs else None},
                                "node metadata signature matches for this job",
                                "read-only MCP snapshot"))
            else:
                g1.append(check("gate1.figma", "Figma node unchanged", "pending",
                                {"nodeId": node_id, "inBaseline": before is not None,
                                 "inFinal": after is not None},
                                "this job nodeId is in both snapshots",
                                "the snapshot does not cover this node; it cannot support PASS"))
        else:
            g1.append(check("gate1.figma", "Figma node unchanged", "pending",
                            "snapshot missing", "one metadata snapshot before and after", ""))

    # ---------- Gate 2 ----------
    g2_ids = ("color.rows", "color.controlProbe", "proof.sourceVsCmyk",
              "proof.cmykVsAi", "pf.bleed", "pf.structure", "pf.tac",
              "ai.cmykSet", "ai.cmykCounts", "evidence.preflight",
              "evidence.proof", "evidence.colorAudit")
    present = {c["id"] for c in checks}
    missing_g2 = [x for x in g2_ids if x not in present]
    missing_check = None
    if missing_g2:
        missing_check = check("gate2.missing", "Gate 2 required evidence", "pending",
                              missing_g2, "all present", "missing evidence cannot PASS")
        checks.append(missing_check)
    g2_checks = [c for c in checks if c["id"] in g2_ids]
    if missing_check:
        g2_checks.append(missing_check)
    g2_status = "fail" if any(c["status"] == "fail" for c in g2_checks) else (
        "pending" if any(c["status"] == "pending" for c in g2_checks) else "pass")
    g2 = [check("gate2.summary", "conversion consistency", g2_status,
                {c["id"]: c["status"] for c in g2_checks}, "all pass", "")]

    # ---------- Gate 3 ----------
    final_pdf_path = os.path.join(job_dir, job["output"]["pdf"]) if job.get("output", {}).get("pdf") else None
    final_hash_now = sha(final_pdf_path)
    gate3_fresh = (gate3 and final_hash_now
                   and gate3.get("sha256") == final_hash_now
                   and gate3.get("aiSha256") == ai_hash
                   and gate3.get("acceptanceHash") == acceptance.digest(job, spec))
    if gate3_fresh:
        g3 = [check("gate3.finalPdf", "final PDF", gate3.get("overall", "pending"),
                    gate3.get("counts"), "all pass",
                    "see gate3.json (hash matches the current PDF/AI)")]
    else:
        g3 = [check("gate3.finalPdf", "final PDF", "pending",
                    "not generated, not checked, or gate3.json is stale",
                    "export by hand, then run 08-gate3.py",
                    "the agent does not produce the deliverable PDF; a stale gate3.json cannot vouch for a new file")]

    # ---------- overall ----------
    all_checks = g1 + checks + g2 + g3
    fails = [c for c in all_checks if c["status"] == "fail"]
    pend = [c for c in all_checks if c["status"] == "pending"]
    warns = [c for c in all_checks if c["status"] == "warn"]
    overall = "fail" if fails else ("pending" if pend else ("warn" if warns else "pass"))

    qa = {
        "jobId": job_id,
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "overall": overall,
        "counts": {"pass": sum(1 for c in all_checks if c["status"] == "pass"),
                   "fail": len(fails), "pending": len(pend), "warn": len(warns)},
        "spec": {
            "icc": spec["print"]["iccName"],
            "pdfx": spec["print"].get("pdfxStandard"),
            "bleedMm": spec["print"].get("bleedMm"),
            "trimMm": [tw, th] if tw else None,
            "renderingIntent": spec["print"].get("renderingIntentName"),
            "blackPointCompensation": spec["print"].get("blackPointCompensation"),
            "tacLimitPercent": spec["print"].get("tacLimitPercent"),
        },
        "gates": {"gate1": "fail" if any(c["status"] == "fail" for c in g1) else
                            ("pending" if any(c["status"] == "pending" for c in g1) else "pass"),
                  "gate2": g2_status,
                  "gate3": g3[0]["status"]},
        "checks": all_checks,
        "hashes": {"source": src_hash, "cmyk": cmyk_hash, "ai": ai_hash,
                   "config": cfg_hash, "targetICC": target_hash},
        "approval": {"required": True, "approvedBy": None, "approvedAt": None,
                     "note": "PASS covers software evidence only; print colour and contract proof stay PENDING."},
    }

    # Internal prepress scope: checks we can execute here. Printer marks,
    # contract proof, brand tolerance and GUI-only profile/unit checks stay
    # external and do not block the internal "prepress done" statement.
    external_ids = {
        "gate1.figma",  # provenance snapshot, not a print defect
        "ai.colorProfile", "ai.strokeWidth", "ai.blackPolicy",
        "pf.tac", "pf.neutral", "color.tolerance", "proof.illustrator",
    }
    # Gate 3 is external only when a candidate exists and every PENDING
    # sub-check is a printer-policy item. A software PENDING stays internal,
    # and a missing candidate is always internal.
    g3_pending = [c["id"] for c in (gate3 or {}).get("checks", [])
                  if c.get("status") == "pending"]
    gate3_present = bool((gate3 or {}).get("checks"))
    gate3_external_only = bool(gate3_fresh) and gate3_present and set(g3_pending) <= {
        "pdf.pdfx", "pdf.tac", "pdf.separations", "pdf.overprint"}
    external_pending = []
    internal_pending = []
    for c in pend:
        if c["id"] == "gate3.finalPdf":
            (external_pending if gate3_external_only else internal_pending).append(c["id"])
        elif c["id"] in external_ids:
            external_pending.append(c["id"])
        else:
            internal_pending.append(c["id"])
    qa["internalPrepress"] = {
        "status": "pass" if not fails and not internal_pending else
                  ("fail" if fails else "pending"),
        "note": "prepress checks runnable here; external items are listed apart from print shop / GUI / contract proof.",
        "internalPending": internal_pending,
        "externalPending": external_pending,
    }
    with open(os.path.join(job_dir, "qa.json"), "w", encoding="utf-8") as fh:
        json.dump(qa, fh, ensure_ascii=False, indent=2)

    icon = {"pass": "PASS", "warn": "WARN", "fail": "FAIL", "pending": "PENDING"}
    lines = [
        "# QA — %s" % job_id, "",
        "Verdict: **%s** · %d pass / %d fail / %d pending / %d warn" % (
            overall.upper(), qa["counts"]["pass"], qa["counts"]["fail"],
            qa["counts"]["pending"], qa["counts"]["warn"]),
        "",
        "Gate 1 source %s · Gate 2 conversion %s · Gate 3 output %s" % (
            qa["gates"]["gate1"], qa["gates"]["gate2"], qa["gates"]["gate3"]),
        "",
        "Spec: %s · PDF/X: %s · bleed %s mm · trim %s×%s mm" % (
            spec["print"]["iccName"], spec["print"].get("pdfxStandard"),
            spec["print"].get("bleedMm"), tw, th),
        "",
        "| | Check | Measured | Expected | Basis |", "|---|---|---|---|---|",
    ]
    for c in qa["checks"]:
        val = json.dumps(c["value"], ensure_ascii=False) if not isinstance(c["value"], str) else c["value"]
        lines.append("| %s | %s | %s | %s | %s |" % (
            icon.get(c["status"], c["status"]), c["label"], val, c["expected"], c["evidence"]))
    if fails:
        lines += ["", "## Blockers (do not deliver)", ""]
        for c in fails:
            lines.append("- **%s**: %s (expected %s)" % (c["label"], c["value"], c["expected"]))
    if pend:
        lines += ["", "## PENDING (missing evidence or external confirmation)", ""]
        for c in pend:
            lines.append("- **%s**: %s (expected %s)%s" % (c["label"], c["value"], c["expected"], c["evidence"]))
    lines += ["", "---", "",
              "This report is not print acceptance. `07c` produces the candidate PDF and Gate 3 runs right after it;",
              "final release = Gate 3 + written print shop confirmation + human approval. Cite the evidence source when claiming PDF/X.",
              "ΔE00 is an ICC prediction, not a physical measurement; the contract proof remains the final basis.", ""]
    with open(os.path.join(job_dir, "qa.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))

    _bleed_mm = spec['print'].get('bleedMm', 0)
    _trim_label = ("%s × %s mm" % (tw, th)) if tw else "unresolved (no prepare.json)"
    _bleedbox_label = ("%s × %s mm" % (tw + 2 * _bleed_mm, th + 2 * _bleed_mm)) if tw else "unresolved"
    checklist = f"""# Candidate PDF acceptance checklist

Candidate PDF generated by `07c-finalize-pdf.py` from `work/cmyk-final.pdf`: `{job.get("output", {}).get("pdf", "")}`. The PDF is the print-shop input. `{job.get("output", {}).get("ai", "")}` is a reference copy for artwork checks; it has no document CMYK profile. Do not export by hand again; use this checklist for acceptance.

Trim: **{_trim_label}**; bleed: **{_bleed_mm} mm**; page boxes: TrimBox {_trim_label} / BleedBox {_bleedbox_label}.
Document ICC: **{spec["print"].get("iccName")}** (DestOutputProfile = GTS_PDFX OutputIntent); TAC limit: **{spec["print"].get("tacLimitPercent")}%**.

- [ ] Confirm the print-shop input is the PDF; keep the `.ai` as a reference copy.
- [ ] Confirm PDF page count = 1 and MediaBox includes all Japanese double-line corner marks and the cross mark at the midpoint of each side.
- [ ] Confirm TrimBox = {tw} × {th} mm and BleedBox extends {spec['print'].get('bleedMm')} mm on each side.
- [ ] Confirm the marks are Japanese double-line: inner/outer corner register marks + midpoint crosses, 24 line segments, 0.3 pt stroke, no colour bar.
- [ ] Check in Acrobat output preview: inner/outer register marks and crosses lie outside TrimBox; inner marks touch the trim line, outer marks touch the 3 mm bleed line.
- [ ] Confirm the document ICC is **{spec["print"].get("iccName")}** and is the DestOutputProfile of GTS_PDFX OutputIntent; the label matches the actual conversion (hash binding in pdf-finalize.json).
- [ ] Confirm page content has no RGB operators and no unembedded fonts; a Type3 shell with 0 painting operators in CharProcs is acceptable.
- [ ] Run `python3 scripts/08-gate3.py {job_id}` and confirm 0 FAIL; Gate 3 decides the PDF/X-4 structure self-check and the TAC limit ({spec["print"].get("tacLimitPercent")}%).
- [ ] Measured TAC is in `preflight.json`; the limit is {spec["print"].get("tacLimitPercent")}% (spec.print.tacLimitPercent). Over the limit, use correct conversion or ink limiting, never clip or round.
- [ ] Write the final PDF hash, the Gate 3 result and the print shop confirmation back to `gate3.json` before sending to print.

## Still needs print shop confirmation

- Default is PDF/X-4:2010 (spec.print.pdfxStandard); a written print shop requirement wins when it differs.
- TAC limit {spec["print"].get("tacLimitPercent")}% (spec.print.tacLimitPercent); the current visible measurement is in preflight.json.
- Whether paper, printing process and ICC match {spec["print"].get("iccName")}.
- Black plate / trapping policy; body text in this job is four-colour black/grey.
- Contract proof and ΔE00 tolerance.

## Master file boundary

For a Figma job, Figma is the editable master. For a direct sRGB PDF job, the source PDF is the master. The `.ai` and the candidate PDF are outlined production files; outlining text is irreversible.
The agent does not modify `source/`, does not write back to Figma, and does not claim the candidate PDF as the final deliverable.
"""
    with open(os.path.join(job_dir, "checklist.md"), "w", encoding="utf-8") as fh:
        fh.write(checklist)

    print(json.dumps({"overall": overall, "counts": qa["counts"],
                      "gates": qa["gates"]}, ensure_ascii=False))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
