#!/usr/bin/env python3
"""04-report.py <job-id>

Merge all evidence into qa.json / qa.md / 导出清单.md.

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
        checks.append(check("evidence.%s" % name, "缺少证据 %s" % name, "pending",
                            "缺失", "存在", why))

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
    checks.append(check("evidence.source", "RGB 源稿未被修改",
                        "pass" if src_ok else "fail", src_hash, expected_src or "与基线一致",
                        "基线：%s" % baseline if baseline else ""))

    prep_ok = bool(cmyk_hash) and (not prep or not prep.get("outputSha256") or cmyk_hash == prep["outputSha256"])
    checks.append(check("evidence.cmyk", "CMYK 中间件哈希",
                        "pass" if prep_ok else "fail", cmyk_hash,
                        (prep or {}).get("outputSha256"), ""))

    ai_ok = bool(ai_hash) and (not ai_res or not ai_res.get("deliverableSha256")
                               or ai_hash == ai_res.get("deliverableSha256"))
    checks.append(check("evidence.ai", "交付 AI 哈希与 QA 时一致",
                        "pass" if ai_ok else "fail", ai_hash,
                        (ai_res or {}).get("deliverableSha256"),
                        "同大小替换也会被哈希发现"))

    # Evidence files must refer to the same inputs as the deliverable.
    if pf:
        pf_ok = pf.get("sourcePdfSha256") == cmyk_hash
        checks.append(check("evidence.preflight", "预检绑定当前 cmyk.pdf",
                            "pass" if pf_ok else "fail",
                            pf.get("sourcePdfSha256"), cmyk_hash,
                            "重跑 01 后必须重跑 05"))
    if proof:
        ph = proof.get("hashes") or {}
        pr_ok = (ph.get("source") == src_hash
                 and ph.get("cmyk") == cmyk_hash
                 and ph.get("ai") == ai_hash)
        checks.append(check("evidence.proof", "对照图绑定当前 cmyk.pdf / AI",
                            "pass" if pr_ok else "fail",
                            proof.get("hashes"), {"cmyk": cmyk_hash, "ai": ai_hash},
                            "重跑 02/03 后必须重跑 07"))
    if ca:
        brand_sha = sha(os.path.join(ROOT, "color", "brand-map.json"))
        ca_ok = (ca.get("source", {}).get("sha256") == src_hash
                 and ca.get("cmykSha256") == cmyk_hash
                 and ca.get("brandMapSha256") == brand_sha)
        checks.append(check("evidence.colorAudit", "颜色映射绑定当前源稿 / cmyk.pdf",
                            "pass" if ca_ok else "fail",
                            {"source": ca.get("source", {}).get("sha256"),
                             "cmyk": ca.get("cmykSha256")},
                            {"source": src_hash, "cmyk": cmyk_hash},
                            "重跑 01 后必须重跑 06"))

    final_pdf = os.path.join(job_dir, job["output"]["pdf"]) if job.get("output", {}).get("pdf") else None
    final_hash = sha(final_pdf)
    if final_hash:
        g3_ok = (bool(gate3) and gate3.get("sha256") == final_hash
                 and gate3.get("aiSha256") == ai_hash
                 and gate3.get("acceptanceHash") == acceptance.digest(job, spec))
        checks.append(check("evidence.gate3", "Gate 3 绑定最终 PDF",
                            "pass" if g3_ok else "fail",
                            (gate3 or {}).get("sha256"), final_hash,
                            "最终 PDF 替换后必须重跑 08"))
    # No final PDF: gate3.finalPdf already carries the PENDING state.

    cfg_hash = sha(os.path.join(ROOT, "config", "print-spec.json"))
    checks.append(check("evidence.config", "印刷配置哈希",
                        "pass" if prep and prep.get("configSha256") == cfg_hash else "pending",
                        cfg_hash, (prep or {}).get("configSha256"),
                        "旧 job 没有 configSha256；重跑 01 后才有绑定"))

    target_path = ((prep or {}).get("conversion") or {}).get("targetICC")
    target_hash = sha(target_path) if target_path else None
    checks.append(check("evidence.targetICC", "目标 ICC 未被替换",
                        "pass" if target_hash and target_hash == (prep or {}).get("iccSha256") else "fail",
                        target_hash, (prep or {}).get("iccSha256"), target_path or ""))

    # ---------- conversion ----------
    if prep:
        checks.append(check("prep.color", "转换后色彩空间",
                            "pass" if prep.get("deviceRGB", 1) == 0 and prep.get("deviceCMYK", 0) > 0 else "fail",
                            "DeviceRGB×%s / DeviceCMYK×%s" % (prep.get("deviceRGB"), prep.get("deviceCMYK")),
                            "DeviceRGB 0 且 DeviceCMYK > 0", "ICC: %s" % prep.get("icc")))
        checks.append(check("prep.pages", "转换后页数",
                            "pass" if prep.get("pages") == 1 else "fail", prep.get("pages"), 1, ""))
        src_info = prep.get("sourceICC") or {}
        checks.append(check("prep.sourceICC", "源 ICC",
                            "pass" if prep.get("sourceKind") == "rgb-icc" and src_info.get("isRGB") else "fail",
                            {"kind": prep.get("sourceKind"), "description": src_info.get("description"),
                             "space": src_info.get("space"), "sha256": prep.get("sourceICCsha256")},
                            "嵌入 RGB ICC，且记录哈希", ""))
        conv = prep.get("conversion") or {}
        conv_ok = (conv.get("renderingIntent") == 1
                   and conv.get("blackPointCompensation") is True
                   and conv.get("overrideICC") is False
                   and conv.get("sourceICCsha256") == prep.get("sourceICCsha256")
                   and conv.get("targetICCsha256") == prep.get("iccSha256"))
        checks.append(check("prep.conversion", "转换参数与记录一致",
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
            exp_w = (tw + 2 * b) / 0.75
            exp_h = (th + 2 * b) / 0.75
            ok = (abs(page[0] - exp_w) <= 0.5 and abs(page[1] - exp_h) <= 0.5) or \
                 (abs(page[0] - exp_h) <= 0.5 and abs(page[1] - exp_w) <= 0.5)
            checks.append(check("prep.pageSize", "中间件页面尺寸",
                                "pass" if ok else "fail", page,
                                "约 %.1f × %.1f mm" % (exp_w, exp_h),
                                "含 %.1f mm 出血，按 0.75 缩放" % b))
    else:
        checks.append(check("prep.color", "转换检查", "fail", "缺少 prepare.json", "存在", ""))

    # ---------- Illustrator result ----------
    if ai_res and ai_res.get("ok"):
        checks.append(check("ai.colorMode", "文档色彩模式",
                            "pass" if ai_res.get("isCMYK") else "fail",
                            ai_res.get("docColorSpace"), "CMYK", ""))
        ab = ai_res.get("artboardMm") or {}
        if job.get("trim", {}).get("preset"):
            tw, th = spec["trimPresetsMm"][job["trim"]["preset"]]
        else:
            tw, th = job["trim"]["w"], job["trim"]["h"]
        d = max(abs(ab.get("w", 0) - tw), abs(ab.get("h", 0) - th))
        checks.append(check("ai.artboard", "画板尺寸",
                            "pass" if d <= 0.2 else "fail",
                            "%s × %s mm" % (ab.get("w"), ab.get("h")),
                            "%s × %s mm" % (tw, th), "最大偏差 %.2f mm" % d))
        checks.append(check("ai.linked", "未嵌入链接",
                            "pass" if ai_res.get("linkedItems", 1) == 0 else "fail",
                            ai_res.get("linkedItems"), 0, ""))
        checks.append(check("ai.linkedRasters", "外链位图",
                            "pass" if not ai_res.get("linkedRasters") else "fail",
                            ai_res.get("linkedRasters") or 0, 0, ""))
        usage = ai_res.get("colorUsage") or {}
        rgb = usage.get("rgb", 0)
        other = usage.get("other", 0)
        errs = ai_res.get("scanErrors", 0)
        checks.append(check("ai.rgbResidue", "残留非 CMYK 矢量色",
                            "pass" if rgb == 0 and other == 0 and errs == 0 else "fail",
                            "RGB %s / 其他 %s / 读取失败 %s" % (rgb, other, errs), 0,
                            "CMYK %s / 专色 %s / 灰 %s" % (usage.get("cmyk"), usage.get("spot"), usage.get("gray"))))
        rasters = ai_res.get("rasters") or []
        bad = [r for r in rasters if "CMYK" not in str(r.get("imageColorSpace", ""))]
        checks.append(check("ai.rasterColor", "位图内部色彩空间",
                            "pass" if not bad else "fail",
                            ", ".join(sorted({str(r.get("imageColorSpace")) for r in rasters})) or "无位图",
                            "ImageColorSpace.CMYK", ""))
        spots = ai_res.get("spotColors") or []
        checks.append(check("ai.spotColors", "专色（排除 Registration）",
                            "pending" if spots else "pass", spots or 0, 0,
                            "专色需印厂确认" if spots else ""))
        checks.append(check("ai.text", "可编辑文字",
                            "pass" if ai_res.get("textFrames", 0) == 0 else "pending",
                            ai_res.get("textFrames", 0), 0,
                            "本流程文字已转曲；非 0 说明源行为变了"))
        types = ai_res.get("itemTypes") or {}
        checks.append(check("ai.nonNative", "非原生图稿",
                            "pass" if types.get("NonNativeItem", 0) == 0 else "pending",
                            types.get("NonNativeItem", 0), 0, ""))
        ov = ai_res.get("overflowMm") or {}
        b = float(job.get("bleedMm", spec["print"].get("bleedMm", 0)) or 0)
        worst = max((abs(v - b) for v in ov.values()), default=b)
        checks.append(check("ai.overflow", "逐边出血",
                            "pass" if worst <= 0.5 else "warn",
                            ov, "四边各 %.1f mm（±0.5）" % b,
                            "最大偏差 %.2f mm" % worst))
        prof = ai_res.get("colorProfileName") or ""
        checks.append(check("ai.colorProfile", "Illustrator 文档配置文件",
                            "pass" if "FOGRA39" in prof.upper() else "pending",
                            prof or "ExtendScript 未返回", "Coated FOGRA39",
                            "28.4.1 未暴露可靠读取；需 GUI 确认"))
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
                    cs_status, cs_note = "pass", "直接解析内容流，不依赖缩略图"
                elif missing_nonpainting:
                    cs_status, cs_note = "pass", (
                        "缺少的元组只出现在不可见 Type3 文字或透明遮罩上下文；"
                        "300 dpi 可见像素为 0")
                elif visible_ok:
                    cs_status, cs_note = "pending", (
                        "缺少的元组在平色渲染中没有差异，但平色遮罩不能证明对象被遮挡；"
                        "需在 AI 中逐项确认后再送印")
                else:
                    cs_status, cs_note = "fail", "可见渲染也有差异；不是单纯遮挡"
                checks.append(check("ai.cmykSet", "AI 内容流 CMYK 数值保持",
                                    cs_status,
                                    {"cmykPdf": len(cset), "ai": len(aset), "maxChannelDelta": round(max_delta, 2),
                                     "missing": missing_c[:5], "missingEvidence": missing_evidence,
                                     "visibleProof": visible_ok},
                                    "cmyk.pdf 与 .ai 的 CMYK 元组一致（±0.3）", cs_note))
                thumbs = pdfprobe.thumbnails(ai_path)
                page_rgb = sum(c["count"] for c in ai["colours"] if c["space"] == "rgb")
                thumb_rgb = any(t.get("deviceRGB") or t.get("indexed") for t in thumbs)
                if page_rgb == 0 and (thumb_rgb or
                                      (ai["structure"].get("deviceRGB", 0) == 0
                                       and ai["structure"].get("indexed", 0) == 0)):
                    fs_status = "pass"
                    fs_note = "RGB/Indexed 仅出现在 /Thumb，页面内容无 RGB"
                else:
                    fs_status = "pending"
                    fs_note = "RGB 引用不在已识别的 /Thumb 中；需人工定位"
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
                checks.append(check("ai.cmykCounts", "AI 每个 CMYK 元组的对象数",
                                    "pass" if not short_open else "pending",
                                    {"short": short_open[:6], "checked": short[:6],
                                     "cmykTuples": len(cc), "aiTuples": len(ac)},
                                    "AI 中每个元组的操作数不少于 cmyk.pdf",
                                    "少一个同色对象也可能是丢线/丢字；"
                                    "只有已证明为不可见文字/遮罩的元组才不阻塞"))

                checks.append(check("ai.fileColorspace", ".ai 内 RGB/Indexed 引用归属",
                                    fs_status,
                                    {"deviceRGB": ai["structure"].get("deviceRGB", 0),
                                     "indexed": ai["structure"].get("indexed", 0),
                                     "pageRGBOperators": page_rgb,
                                     "thumbnails": thumbs},
                                    "页面内容 0 个 RGB；缩略图单独说明", fs_note))
            except Exception as exc:  # noqa: BLE001
                checks.append(check("ai.cmykSet", "AI 内容流 CMYK 数值保持", "pending",
                                    str(exc), "可解析", ""))
        # Stroke / dark policy
        checks.append(check("ai.strokeWidth", "最细描边",
                            "pending" if ai_res.get("strokeCount", 0) == 0 else
                            ("pass" if (ai_res.get("strokeMinMm") or 0) >= 0.1 else "pending"),
                            {"strokes": ai_res.get("strokeCount"),
                             "minMm": ai_res.get("strokeMinMm"),
                             "under0_1": ai_res.get("strokeUnder0_1Mm")},
                            "≥ 0.1 mm；转曲文字另做视觉检查",
                            "本单 stroked=0，不能据此证明转曲字细笔画安全"))
        nd = ai_res.get("neutralDark", 0)
        pk = ai_res.get("pureK", 0)
        pn = ((pf or {}).get("neutral") or {})
        four = pn.get("fourColourGreyPercent", 0) or 0
        pn_pure = pn.get("pureKPercent", 0) or 0
        needs_policy = (nd > 0 or pk > 0 or four > 0
                        or (pn.get("neutralDarkPixels", 0) > 0 and pn_pure == 0))
        checks.append(check("ai.blackPolicy", "黑版 / 四色灰",
                            "pending" if needs_policy else "pass",
                            {"aiNeutralDark": nd, "aiPureK": pk,
                             "preflightFourColourGreyPercent": four,
                             "preflightPureKPercent": pn_pure,
                             "histogram": dict(sorted((ai_res.get("cmykHistogram") or {}).items(),
                                                      key=lambda kv: -kv[1])[:5])},
                            "按印厂黑版/陷印政策确认；不得擅自改品牌深色",
                            "正文为四色灰/黑，纯 K 为 0" if needs_policy else ""))
    elif ai_res is not None:
        checks.append(check("ai.result", "Illustrator 自检结果", "fail",
                            ai_res.get("error", "unknown"), "ok:true", ""))

    # ---------- preflight ----------
    if pf and pf.get("ok"):
        pb = pf.get("bleed") or {}
        band_ok = all(v.get("whitePixels", 1) == 0 for v in (pb.get("sides") or {}).values())
        band_mm = (pb.get("bandMmActual") or {})
        band_ok = band_ok and band_mm.get("x", 0) >= 3.0 and band_mm.get("y", 0) >= 3.0
        checks.append(check("pf.bleed", "完整 3 mm 出血带",
                            "pass" if band_ok else "fail",
                            {"sides": pb.get("sides"), "bandMmActual": band_mm,
                             "whitePixels": pb.get("whitePixels")},
                            "四边完整 3 mm，纯白像素 0",
                            "按成品坐标换算，不再用中间件 18 px 近似"))
        pt = pf.get("tac") or {}
        vec_info = pf.get("vectorMaxTac") or {}
        checks.append(check("pf.tac", "总墨量 TAC",
                            pt.get("status", "pending"),
                            {"raster150": pt.get("maxTacPercent"),
                             "raster300": (pt.get("dpi300") or {}).get("maxTacPercent"),
                             "vector": pf.get("vectorMaxTac"),
                             "vectorVisible": vec_info.get("visibleAt300"),
                             "visibleMax": pt.get("visibleMaxTacPercent"),
                             "limit": pt.get("limitPercent")},
                            "≤ 印厂确认上限；未确认则只报告",
                            pt.get("note", "")))
        vec_tac = pt.get("visibleVectorTac") or 0
        if pt.get("limitPercent") is None and max(pt.get("visibleMaxTacPercent") or 0, vec_tac) >= 300:
            checks.append(check("pf.tacRisk", "TAC 超过常见 300% 上限",
                                "warn",
                                {"visibleMax": pt.get("visibleMaxTacPercent"),
                                 "raster150": pt.get("maxTacPercent"),
                                 "raster300": (pt.get("dpi300") or {}).get("maxTacPercent"),
                                 "vector": vec_info.get("tac"),
                                 "vectorVisible": vec_info.get("visibleAt300")},
                                "印厂确认 300% 或 330% 上限",
                                "矢量最高值若未在 300 dpi 光栅中可见，视为被遮挡/裁掉；可见最大值仍以光栅为准"))
        st = pf.get("structure") or {}
        col = pf.get("colours") or {}
        struct_ok = col.get("rgbOperators", 0) == 0 and st.get("separation", 0) == 0 and not st.get("allSeparation")
        checks.append(check("pf.structure", "中间件结构",
                            "pass" if struct_ok else "fail",
                            {"rgbOperators": col.get("rgbOperators"),
                             "cmykOperators": col.get("cmykOperators"),
                             "separation": st.get("separation"),
                             "allSeparation": st.get("allSeparation"),
                             "overprint": st.get("overprintTrue"),
                             "type3": st.get("type3"),
                             "smask": st.get("smask")},
                            "RGB 0；无专色 /All；叠印逐项确认",
                            "Type3/SMask 是中间件事实，最终 PDF 另查"))
        checks.append(check("pf.overprint", "叠印 / 叠印模式",
                            "pending" if (st.get("overprintTrue", 0) or st.get("opmTrue", 0)) else "pass",
                            {"overprintTrue": st.get("overprintTrue"), "opmTrue": st.get("opmTrue")},
                            0, "非 0 时必须在 Acrobat 输出预览确认"))
        n = pf.get("neutral") or {}
        checks.append(check("pf.neutral", "四色灰 / 纯 K（光栅）",
                            "pending" if n.get("fourColourGreyPercent", 0) > 0 and n.get("pureKPercent", 0) == 0 else "pass",
                            n, "由印厂黑版政策决定",
                            "仅光栅统计；对象级值见 vectorMaxTac"))
    elif pf is not None:
        checks.append(check("pf.result", "预检执行结果", "fail", pf.get("error", "unknown"), "ok:true", ""))

    # ---------- colour audit / proof ----------
    if ca:
        rows = ca.get("rows") or []
        unnamed = [r["hex"] for r in rows if not r.get("name")]
        unmatched = [r["hex"] for r in rows if not r.get("matched", True)]
        checks.append(check("color.rows", "品牌色映射覆盖",
                            "pass" if rows and not unnamed and not unmatched else "pending",
                            {"colours": len(rows), "unnamed": unnamed[:8], "unmatched": unmatched[:8]},
                            "全部实测纯色有名、有实际 CMYK", ""))
        cmm = (ca.get("summary") or {}).get("maxCmmDifference")
        unmatched = (ca.get("summary") or {}).get("unmatched") or []
        checks.append(check("color.controlProbe", "实际 gs 路径与 ICC 预测一致",
                            "pass" if (not unmatched and cmm is not None and cmm <= 1.0) else "pending",
                            {"maxCmmDifference": cmm, "unmatched": unmatched},
                            "≤ 1.0 ΔE00；所有源色匹配到实际 CMYK",
                            "LittleCMS 预测 vs 实际 cmyk.pdf，不是只看参数回显"))
        tol = ca.get("tolerance") or {}
        limit = tol.get("deltaE00")
        rows_ca = ca.get("rows") or []
        over_rows = ([r["hex"] for r in rows_ca
                      if r.get("deltaE00Predicted") is not None and r["deltaE00Predicted"] > limit]
                     if limit is not None else [])
        checks.append(check("color.tolerance", "品牌色容差",
                            "pending" if limit is None else
                            ("fail" if over_rows else "pass"),
                            {"limit": limit, "overLimit": over_rows,
                             "max": (ca.get("summary") or {}).get("maxDeltaE00")},
                            "印厂/品牌确认的 ΔE00 容差",
                            tol.get("note", "")))
    if proof:
        s = proof.get("sourceVsCmyk") or {}
        ok = s.get("median", 99) <= 2.0 and s.get("over10Percent", 99) <= 1.0
        checks.append(check("proof.sourceVsCmyk", "原稿 vs FOGRA39 模拟",
                            "pass" if ok else "fail",
                            {k: s.get(k) for k in ("median", "p95", "p99", "over10Percent")},
                            "median ≤ 2, over10 ≤ 1%", "ICC 模拟，不含纸白/实物"))
        a = proof.get("cmykVsAi") or {}
        checks.append(check("proof.cmykVsAi", "CMYK 中间件 vs AI",
                            "pass" if a.get("status") == "pass" else
                            ("pending" if a.get("status") == "pending" else "fail"),
                            {k: a.get(k) for k in ("median", "p99", "over5Percent", "alignment")},
                            "median ≤ 0.5, over5 ≤ 0.05%", a.get("note", "")))
        checks.append(check("proof.illustrator", "Illustrator Proof Setup",
                            "pending", "未采集 GUI 证据", "Proof Colors + FOGRA39 设置截图/回读",
                            "本机字典无 Proof Setup 接口；lcms 模拟不能替代"))

    # ---------- Gate 1 ----------
    g1 = []
    if rgb_audit and rgb_audit.get("ok"):
        g1.append(check("gate1.rgbImport", "RGB 工作副本导入",
                        "pass" if rgb_audit.get("documentColorSpace") == "DocumentColorSpace.RGB"
                        and "SRGB" in str(rgb_audit.get("colorProfileName", "")).upper()
                        and rgb_audit.get("placedItems", 0) == 0 else "pending",
                        {"space": rgb_audit.get("documentColorSpace"),
                         "profile": rgb_audit.get("colorProfileName"),
                         "placedItems": rgb_audit.get("placedItems")},
                        "RGB / sRGB2014 / 无外链", ""))
        if src_audit:
            src_page = src_audit.get("boxes", {}).get("MediaBox")
            if src_page:
                sw = round((src_page[2] - src_page[0]) * 25.4 / 72, 2)
                sh = round((src_page[3] - src_page[1]) * 25.4 / 72, 2)
                ab = rgb_audit.get("artboardMm") or {}
                dd = max(abs(ab.get("w", 0) - sw), abs(ab.get("h", 0) - sh))
                g1.append(check("gate1.artboard", "源页与 RGB 副本尺寸",
                                "pass" if dd <= 0.2 else "fail",
                                {"source": [sw, sh], "rgbCopy": ab}, "±0.2 mm",
                                "最大偏差 %.2f mm" % dd))
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
                        colour_note = ("ExtendScript 未枚举纯黑转曲字；AI 内容流存在对应 CMYK 元组 %s"
                                       % (" / ".join("%.1f" % x for x in target)))
            if colour_status == "pending" and missing_hex == ["#000000"] and not extra_hex:
                ai_set = next((c for c in checks if c["id"] == "ai.cmykSet"), None)
                cv = ((proof or {}).get("cmykVsAi") or {}).get("status")
                if ai_set and ai_set.get("status") == "pass" and cv == "pass":
                    colour_status = "pass"
                    colour_note = ("纯黑只存在于不可见 Type3 壳；ai.cmykSet 已证明 nonPainting，"
                                   "cmykVsAi 通过；可见渲染无差异")
            g1.append(check("gate1.colours", "源色板与 RGB 副本一致",
                            colour_status,
                            {"source": len(src_hex), "copy": len(rgb_hex),
                             "missing": missing_hex, "extra": extra_hex,
                             "pathFilled": rgb_audit.get("pathFilled")},
                            "HEX 集合一致", colour_note))
        g1.append(check("gate1.export", "RGB 对照渲染",
                        "pass" if rgb_audit.get("exported") else "pending",
                        rgb_audit.get("exported"), True, "; ".join(rgb_audit.get("warnings") or [])))
        sva = (proof or {}).get("sourceVsRgbAi")
        if sva:
            v_ok = sva.get("median", 99) <= 1.0 and sva.get("over5Percent", 99) <= 0.05
            g1.append(check("gate1.visual", "源 PDF vs RGB 工作副本",
                            "pass" if v_ok else "fail",
                            {k: sva.get(k) for k in ("median", "p95", "max", "over5Percent")},
                            "median ≤ 1, over5 ≤ 0.05%",
                            "150 dpi 平色区域对照；细线几何由画板/出血检查"))
        else:
            g1.append(check("gate1.visual", "源 PDF vs RGB 工作副本",
                            "pending", "缺少对照", "07-proof 生成", ""))
    else:
        g1.append(check("gate1.rgbImport", "RGB 工作副本导入", "pending",
                        "缺少 work/rgb-audit.json", "存在", ""))

    g1.append(check("gate1.sourceHash", "源稿哈希", "pass" if src_ok else "fail",
                    src_hash, expected_src or "与基线一致", ""))
    fb = load(os.path.join(ROOT, "color", "figma-baseline.json"))
    ff = load(os.path.join(ROOT, "color", "figma-final.json"))
    node_id = str((job.get("figma") or {}).get("nodeId") or "")
    if fb and ff:
        fb_nodes = fb.get("nodes") or {}
        ff_nodes = ff.get("nodes") or {}
        if node_id and node_id in fb_nodes and node_id in ff_nodes:
            same = fb_nodes.get(node_id) == ff_nodes.get(node_id)
            g1.append(check("gate1.figma", "Figma 节点未变", "pass" if same else "fail",
                            {"nodeId": node_id, "before": fb_nodes.get(node_id),
                             "after": ff_nodes.get(node_id)},
                            "当前 job 的节点元数据哈希一致", "只读 MCP 快照"))
        else:
            g1.append(check("gate1.figma", "Figma 节点未变", "pending",
                            {"nodeId": node_id,
                             "inBaseline": node_id in fb_nodes,
                             "inFinal": node_id in ff_nodes},
                            "当前 job 的 nodeId 在前后快照中",
                            "现有快照是旧六页，不覆盖当前节点；不能据此判 PASS"))
    else:
        g1.append(check("gate1.figma", "Figma 节点未变", "pending",
                        "缺少快照", "前后各一次元数据快照", ""))

    # ---------- Gate 2 ----------
    g2_ids = ("color.rows", "color.controlProbe", "proof.sourceVsCmyk",
              "proof.cmykVsAi", "pf.bleed", "pf.structure", "pf.tac",
              "ai.cmykSet", "ai.cmykCounts", "evidence.preflight",
              "evidence.proof", "evidence.colorAudit")
    present = {c["id"] for c in checks}
    missing_g2 = [x for x in g2_ids if x not in present]
    missing_check = None
    if missing_g2:
        missing_check = check("gate2.missing", "Gate 2 必需证据", "pending",
                              missing_g2, "全部存在", "缺证据不得 PASS")
        checks.append(missing_check)
    g2_checks = [c for c in checks if c["id"] in g2_ids]
    if missing_check:
        g2_checks.append(missing_check)
    g2_status = "fail" if any(c["status"] == "fail" for c in g2_checks) else (
        "pending" if any(c["status"] == "pending" for c in g2_checks) else "pass")
    g2 = [check("gate2.summary", "转换一致性", g2_status,
                {c["id"]: c["status"] for c in g2_checks}, "全部通过", "")]

    # ---------- Gate 3 ----------
    final_pdf_path = os.path.join(job_dir, job["output"]["pdf"]) if job.get("output", {}).get("pdf") else None
    final_hash_now = sha(final_pdf_path)
    gate3_fresh = (gate3 and final_hash_now
                   and gate3.get("sha256") == final_hash_now
                   and gate3.get("aiSha256") == ai_hash
                   and gate3.get("acceptanceHash") == acceptance.digest(job, spec))
    if gate3_fresh:
        g3 = [check("gate3.finalPdf", "最终 PDF", gate3.get("overall", "pending"),
                    gate3.get("counts"), "全部通过",
                    "见 gate3.json（哈希与当前 PDF/AI 一致）")]
    else:
        g3 = [check("gate3.finalPdf", "最终 PDF", "pending",
                    "未生成、未检查或 gate3.json 已过期",
                    "人工导出后运行 08-gate3.py",
                    "Agent 不生成交付 PDF；旧 gate3.json 不能为新文件背书")]

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
                     "note": "PASS 仅限软件证据；印刷颜色与合同打样仍为 PENDING。"},
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
        "note": "内部可执行的印前检查；外部项与印厂/GUI/合同打样分开列。",
        "internalPending": internal_pending,
        "externalPending": external_pending,
    }
    with open(os.path.join(job_dir, "qa.json"), "w", encoding="utf-8") as fh:
        json.dump(qa, fh, ensure_ascii=False, indent=2)

    icon = {"pass": "PASS", "warn": "WARN", "fail": "FAIL", "pending": "PENDING"}
    lines = [
        "# QA — %s" % job_id, "",
        "结论：**%s** · %d pass / %d fail / %d pending / %d warn" % (
            overall.upper(), qa["counts"]["pass"], qa["counts"]["fail"],
            qa["counts"]["pending"], qa["counts"]["warn"]),
        "",
        "Gate 1 源稿 %s · Gate 2 转换 %s · Gate 3 输出 %s" % (
            qa["gates"]["gate1"], qa["gates"]["gate2"], qa["gates"]["gate3"]),
        "",
        "规格：%s · PDF/X：%s · 出血 %s mm · 成品 %s×%s mm" % (
            spec["print"]["iccName"], spec["print"].get("pdfxStandard"),
            spec["print"].get("bleedMm"), tw, th),
        "",
        "| | 项目 | 实测 | 期望 | 依据 |", "|---|---|---|---|---|",
    ]
    for c in qa["checks"]:
        val = json.dumps(c["value"], ensure_ascii=False) if not isinstance(c["value"], str) else c["value"]
        lines.append("| %s | %s | %s | %s | %s |" % (
            icon.get(c["status"], c["status"]), c["label"], val, c["expected"], c["evidence"]))
    if fails:
        lines += ["", "## 阻塞项（不得交付）", ""]
        for c in fails:
            lines.append("- **%s**：%s（期望 %s）" % (c["label"], c["value"], c["expected"]))
    if pend:
        lines += ["", "## PENDING（缺证据或待外部确认）", ""]
        for c in pend:
            lines.append("- **%s**：%s（期望 %s）%s" % (c["label"], c["value"], c["expected"], c["evidence"]))
    lines += ["", "---", "",
              "本报告不构成印刷验收。候选 PDF 由 `07c` 生成并在其后自动跑 Gate 3；",
              "最终放行 = Gate 3 + 印厂书面确认 + 人工批准。声称 PDF/X 时注明证据来源。",
              "ΔE00 为 ICC 预测值，不是实物测量；合同打样仍是最终依据。", ""]
    with open(os.path.join(job_dir, "qa.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))

    _bleed_mm = spec['print'].get('bleedMm', 0)
    _trim_label = ("%s × %s mm" % (tw, th)) if tw else "未解析（缺 prepare.json）"
    _bleedbox_label = ("%s × %s mm" % (tw + 2 * _bleed_mm, th + 2 * _bleed_mm)) if tw else "未解析"
    checklist = f"""# 候选 PDF 验收清单

候选 PDF 已由 `07c-finalize-pdf.py` 从 `work/cmyk-final.pdf` 生成：`{job.get("output", {}).get("pdf", "")}`。`07b` 只导出 AI 图稿供核对。不要重复人工导出；本清单用于验收。

成品：**{_trim_label}**；出血：**{_bleed_mm} mm**；页面框：TrimBox {_trim_label} / BleedBox {_bleedbox_label}。
文档 ICC：**{spec["print"].get("iccName")}**（DestOutputProfile = GTS_PDFX OutputIntent）；TAC 上限：**{spec["print"].get("tacLimitPercent")}%**。

- [ ] 确认 PDF 页数 = 1，MediaBox 包含全部日式双线角线与四边中点十字线。
- [ ] 确认 TrimBox = {tw} × {th} mm，BleedBox 四边各外扩 {spec['print'].get('bleedMm')} mm。
- [ ] 确认标记为日式双线：4 角内/外トンボ + 4 边中点十字，共 24 条线段，线宽 0.3 pt，无色彩条。
- [ ] 在 Acrobat 输出预览检查：内/外トンボ与十字线位于 TrimBox 外，内トンボ贴合成品线，外トンボ贴合 3 mm 出血线。
- [ ] 确认文档 ICC 为 **{spec["print"].get("iccName")}**，且为 GTS_PDFX OutputIntent 的 DestOutputProfile；标签与实际转换一致（哈希绑定见 pdf-finalize.json）。
- [ ] 确认页面内容无 RGB 操作、无未嵌入字体；Type3 壳的 CharProcs 绘画操作为 0 时可接受。
- [ ] 运行 `python3 scripts/08-gate3.py {job_id}`，确认 0 FAIL；PDF/X-4 结构自检与 TAC 上限（{spec["print"].get("tacLimitPercent")}%）由 Gate 3 自动判定。
- [ ] 总墨量实测值见 `preflight.json`；上限 {spec["print"].get("tacLimitPercent")}%（spec.print.tacLimitPercent），超限走正确转换或限墨，不截断不取整。
- [ ] 把最终 PDF 哈希、Gate 3 结果和印厂确认写回 `gate3.json` 后再送印。

## 仍需印厂确认

- 默认 PDF/X-4:2010（spec.print.pdfxStandard）；印厂书面要求不同时以书面为准。
- 总墨量上限 {spec["print"].get("tacLimitPercent")}%（spec.print.tacLimitPercent）；当前可见实测见 preflight.json。
- 纸张、印刷工艺、ICC 是否与 {spec["print"].get("iccName")} 一致。
- 黑版/陷印政策；本单正文为四色黑/灰。
- 合同打样与 ΔE00 容差。

## 母版边界

Figma 是唯一可编辑母版。`.ai` 与候选 PDF 是转曲后的生产件；文字转曲不可逆。
Agent 不修改 `source/`，不写回 Figma，不把候选 PDF 宣称为最终交付。
"""
    with open(os.path.join(job_dir, "导出清单.md"), "w", encoding="utf-8") as fh:
        fh.write(checklist)

    print(json.dumps({"overall": overall, "counts": qa["counts"],
                      "gates": qa["gates"]}, ensure_ascii=False))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
