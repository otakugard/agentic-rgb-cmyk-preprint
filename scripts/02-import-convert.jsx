#target illustrator

/*
 * 02-import-convert.jsx
 * 把 work/cmyk.pdf（已由 01-prepare.sh 转成 Coated FOGRA39）变成 <job-id>.ai。
 *
 * 前置条件：01-prepare.sh 已经跑过。本脚本不再做任何色彩转换——
 * 颜色在 Ghostscript 那一步就定死了，这里只做尺寸与结构。
 *
 * 路线（实测确定）：
 *   1. app.open(work/cmyk.pdf) 走原生 PDF 导入，得到可编辑的矢量图稿
 *   2. 整体缩放到真实成品尺寸（Figma 1px=1pt，固定 0.75）
 *   3. 对齐画板、存 print.ai
 *
 * 不用「置入 + embed」：实测会把整页变成 1 个 NonNativeItem，颜色停在 RGB。
 * 不用 doc-color-cmyk：它依赖 Illustrator 当前工作 CMYK 空间，不能保证是 FOGRA39。
 *
 * 输入（run.sh 注入）：JOB_DIR、SPEC_PATH
 * 输出：<JOB_DIR>/<job-id>.ai、<JOB_DIR>/result.json
 */

var RESULT_PATH = JOB_DIR + "/result.json";
var warnings = [];
var result = { ok: false, script: "02-import-convert", jobId: null, warnings: warnings };
var doc = null;
var prevInteraction = null;

/* ---------- 工具 ---------- */

function esc(s) {
  s = String(s);
  s = s.replace(/\\/g, "\\\\").replace(/"/g, '\\"');
  s = s.replace(/\n/g, "\\n").replace(/\r/g, "\\r").replace(/\t/g, "\\t");
  return s;
}
function jsonStr(v) {
  if (v === null || v === undefined) return "null";
  var t = typeof v;
  if (t === "number") return isFinite(v) ? String(v) : "null";
  if (t === "boolean") return v ? "true" : "false";
  if (t === "string") return '"' + esc(v) + '"';
  if (v instanceof Array) {
    var a = [];
    for (var i = 0; i < v.length; i++) a.push(jsonStr(v[i]));
    return "[" + a.join(",") + "]";
  }
  var p = [];
  for (var k in v) if (v.hasOwnProperty(k)) p.push('"' + esc(k) + '":' + jsonStr(v[k]));
  return "{" + p.join(",") + "}";
}
function readText(path) {
  var f = new File(path);
  if (!f.exists) throw new Error("file not found: " + path);
  f.encoding = "UTF-8";
  if (!f.open("r")) throw new Error("cannot open for read: " + path);
  var s = f.read();
  f.close();
  return s;
}
function readJSON(path) { return eval("(" + readText(path) + ")"); }
function writeText(path, s) {
  var f = new File(path);
  f.encoding = "UTF-8";
  if (!f.open("w")) throw new Error("cannot open for write: " + path);
  f.write(s);
  f.close();
}
function finish(ok, extra) {
  result.ok = ok;
  if (extra) for (var k in extra) if (extra.hasOwnProperty(k)) result[k] = extra[k];

  var tokenFile = new File(JOB_DIR + "/.run-token");
  if (tokenFile.exists) {
    try { result.runToken = readText(JOB_DIR + "/.run-token").replace(/[\r\n]/g, ""); } catch (e0) {}
  }
  writeText(RESULT_PATH, jsonStr(result));
}
function mm2pt(mm) { return mm * 72 / 25.4; }
function pt2mm(pt) { return pt * 25.4 / 72; }
function r2(v) { return Math.round(v * 100) / 100; }
function closeEnough(a, b, tol) { return Math.abs(a - b) <= tol; }

function unionBounds(collection) {
  var l = Infinity, t = -Infinity, r = -Infinity, b = Infinity;
  for (var i = 0; i < collection.length; i++) {
    var gb = collection[i].geometricBounds;
    if (gb[0] < l) l = gb[0];
    if (gb[1] > t) t = gb[1];
    if (gb[2] > r) r = gb[2];
    if (gb[3] < b) b = gb[3];
  }
  return { left: l, top: t, right: r, bottom: b };
}

/* 输出路径必须留在本任务目录内，且不能落进 source/。
   先做路径归一化：`./source/x.ai` 这种写法会绕过朴素的前缀判断。 */
function normalizeRel(rel) {
  var parts = String(rel).split("/");
  var out = [];
  for (var i = 0; i < parts.length; i++) {
    var p = parts[i];
    if (p === "" || p === ".") continue;
    if (p === "..") throw new Error("output.ai 不能含 ..: " + rel);
    out.push(p);
  }
  return out.join("/");
}

function safeOutput(rel) {
  if (!rel) throw new Error("job.json 缺少 output.ai");
  if (String(rel).charAt(0) === "/") throw new Error("output.ai 必须是相对路径: " + rel);
  var norm = normalizeRel(rel);
  if (norm.indexOf("source/") === 0) throw new Error("output.ai 不能写进 source/: " + rel);
  if (norm === "") throw new Error("output.ai 归一化后为空: " + rel);
  return JOB_DIR + "/" + norm;
}

/* ---------- 主流程 ---------- */

try {
  var spec = readJSON(SPEC_PATH);
  var job = readJSON(JOB_DIR + "/job.json");
  result.jobId = job.jobId || null;

  /* 输出路径要在动 Illustrator 之前校验完 */
  var outPath = safeOutput(job.output && job.output.ai);

  /* 成品尺寸：job 里同时给了 preset 和 w/h 时，以 w/h 为准（与 04-report 保持一致） */
  var wmm, hmm;
  if (job.trim && job.trim.w && job.trim.h) {
    wmm = job.trim.w; hmm = job.trim.h;
  } else if (job.trim && job.trim.preset && spec.trimPresetsMm[job.trim.preset]) {
    var preset = spec.trimPresetsMm[job.trim.preset];
    wmm = preset[0]; hmm = preset[1];
  } else {
    throw new Error("job.json 的 trim 无法解析（需要 preset 或 w/h）");
  }

  /* 画板含出血：job.bleedMm 优先，否则用 spec.print.bleedMm。
     源 PDF 的页面 = 成品 + 2×出血；画板只设成品，图稿外扩的部分就是出血。 */
  var bleedMm = (job.bleedMm !== undefined && job.bleedMm !== null)
    ? Number(job.bleedMm) : Number(spec.print.bleedMm || 0);
  if (!isFinite(bleedMm) || bleedMm < 0) bleedMm = 0;
  var frameW = wmm + 2 * bleedMm;
  var frameH = hmm + 2 * bleedMm;

  /* 输入：优先 work/cmyk.pdf，缺失就明确报错，不偷偷回退到 RGB 原件 */
  var prepPath = JOB_DIR + "/prepare.json";
  var inputRel = "work/cmyk.pdf";
  /* 可选：01-prepare 已把几何缩到成品尺寸时，用它直接导入，避免在
     Illustrator 里对多面板裁切组再做一次 group/resize。 */
  if (job.scaleInPdf && new File(JOB_DIR + "/work/cmyk-final.pdf").exists) {
    inputRel = "work/cmyk-final.pdf";
  }
  if (!(new File(JOB_DIR + "/" + inputRel)).exists) {
    throw new Error("缺少 work/cmyk.pdf —— 先跑 scripts/01-prepare.sh " + (job.jobId || jobId));
  }
  if (new File(prepPath).exists) {
    var prep = readJSON(prepPath);
    if (prep.deviceRGB && prep.deviceRGB > 0) {
      warnings.push("prepare 阶段产物仍含 DeviceRGB " + prep.deviceRGB + " 处");
    }
  }

  prevInteraction = app.userInteractionLevel;
  app.userInteractionLevel = UserInteractionLevel.DONTDISPLAYALERTS;

  /* 记录打开前的文档列表：如果 app.open 复用了用户已经开着的文档，
     后面我们会解锁、缩放、另存，等于动用户的活。宁可停下。 */
  var preOpenNames = [];
  for (var di = 0; di < app.documents.length; di++) {
    try { preOpenNames.push(String(app.documents[di].fullName)); } catch (eD) {}
  }

  doc = app.open(new File(JOB_DIR + "/" + inputRel));
  if (!doc) throw new Error("打开 work/cmyk.pdf 失败");

  var openedName = "";
  try { openedName = String(doc.fullName); } catch (eN) {}
  for (var pi = 0; pi < preOpenNames.length; pi++) {
    if (openedName !== "" && preOpenNames[pi] === openedName) {
      doc = null; // 不能关用户自己的文档
      throw new Error("work/cmyk.pdf 已经在你打开的窗口里。请先关闭它再跑，避免脚本改动未保存的内容。");
    }
  }

  doc.rulerUnits = RulerUnits.Millimeters;

  if (doc.documentColorSpace !== DocumentColorSpace.CMYK) {
    throw new Error("打开后不是 CMYK（当前 " + String(doc.documentColorSpace) +
      "）。01-prepare 没有正确转换，或 Ghostscript 用了错误的 ICC。");
  }

  /* 多图层不猜：跨图层重新编组会改变叠放顺序，宁可停下 */
  var usableLayers = [];
  for (var li = 0; li < doc.layers.length; li++) {
    if (doc.layers[li].pageItems.length > 0) usableLayers.push(doc.layers[li]);
  }
  if (usableLayers.length > 1) {
    throw new Error("源文件有 " + usableLayers.length + " 个图层，本流程只支持单图层。" +
      "请先在 Illustrator 里合并图层，或改流程（跨层编组会改变叠放顺序）。");
  }
  if (usableLayers.length === 0) throw new Error("源 PDF 没有图稿内容");

  for (var ui = 0; ui < doc.pageItems.length; ui++) {
    try { doc.pageItems[ui].locked = false; } catch (e1) {}
    try { doc.pageItems[ui].hidden = false; } catch (e2) {}
  }

  /* 方向：允许横竖互换，不允许非等比拉伸。比较的是含出血的源页面与目标页面。 */
  var ab = doc.artboards[0].artboardRect;
  var srcW = ab[2] - ab[0];
  var srcH = ab[1] - ab[3];
  var srcAspect = srcW / srcH;
  var targetAspect = frameW / frameH;
  var tol = 0.005;

  if (closeEnough(srcAspect, targetAspect, tol)) {
    // 一致
  } else if (closeEnough(srcAspect, 1 / targetAspect, tol)) {
    var swap = wmm; wmm = hmm; hmm = swap;
    var swapB = frameW; frameW = frameH; frameH = swapB;
    targetAspect = frameW / frameH;
    warnings.push("源文件横竖反向，成品尺寸按内容方向取为 " + wmm + "×" + hmm + "mm");
  } else {
    throw new Error("长宽比不匹配：源 " + r2(srcAspect) + "，目标 " + r2(targetAspect) +
      "（含出血 " + frameW + "×" + frameH + "mm，成品 " + wmm + "×" + hmm + "mm）。不做非等比拉伸。");
  }

  var targetWpt = mm2pt(frameW);
  var targetHpt = mm2pt(frameH);
  var factorPct = targetWpt / srcW * 100;

  var alreadyFinal = closeEnough(factorPct, 100, 0.1);
  var layer = usableLayers[0];
  var s = factorPct / 100;

  if (!alreadyFinal) {
    /* Figma 1px=1pt 的契约检查：偏离就报警，不静默接受 */
    var expected = spec.figma.pxToPt * 100;
    if (!closeEnough(factorPct, expected, 0.5)) {
      warnings.push("缩放系数 " + r2(factorPct) + "% 与 spec.figma.pxToPt(" + expected +
        "%) 不一致，说明源 PDF 尺寸不符合 Figma 1px=1pt 约定");
    }

    /* 整层编组后缩放：逐个 resize 会各自绕自身中心，版面会散 */
    var snapshot = [];
    for (var si = 0; si < layer.pageItems.length; si++) snapshot.push(layer.pageItems[si]);

    var preUnion = unionBounds(doc.pageItems);
    var grp = layer.groupItems.add();
    for (var mi = 0; mi < snapshot.length; mi++) {
      snapshot[mi].move(grp, ElementPlacement.PLACEATEND);
    }
    /* 第 7 个参数是线宽缩放百分比，必须是数值；传 true 会被当成 1% */
    grp.resize(factorPct, factorPct, true, true, true, true, factorPct);

    try {
      doc.selection = null;
      grp.selected = true;
      app.executeMenuCommand("ungroup");
      doc.selection = null;
    } catch (e4) {
      warnings.push("缩放后未能解除编组：图稿为单一编组，不影响印刷，只是不便逐项编辑");
    }

    /* resize 围绕的锚点不保证是原点（实测会偏移），按"缩放前的包围盒 × 比例"校正回来。
       这样保留原有的页边留白，不会把图稿硬贴到画板角上。 */
    s = factorPct / 100;
    var postUnion = unionBounds(doc.pageItems);
    var dx = preUnion.left * s - postUnion.left;
    var dy = preUnion.top * s - postUnion.top;
    if (Math.abs(dx) > 0.01 || Math.abs(dy) > 0.01) {
      for (var tl = 0; tl < doc.layers.length; tl++) {
        var kids = doc.layers[tl].pageItems;
        for (var ki = 0; ki < kids.length; ki++) kids[ki].translate(dx, dy);
      }
    }
  }

  /* 画板 = 成品矩形，居中在图稿内；图稿外扩 bleedMm 就是出血。
     不做包围盒对齐，否则会把原本的页边留白吃掉 */
  var bPt = mm2pt(bleedMm);
  var newW = mm2pt(wmm);
  var newH = mm2pt(hmm);
  doc.artboards[0].artboardRect = [bPt, bPt + newH, bPt + newW, bPt];

  /* 注：Illustrator 28.4.1 的 ExtendScript 没有可写的 documentBleed 属性
     （实测读取即抛错），出血 3 mm 只能在导出前由人工在「文档设置」里填。
     导出清单已列为必勾项。 */

  var bb = unionBounds(doc.pageItems);
  var overflowMm = {
    left: r2(pt2mm(bPt - bb.left)),
    top: r2(pt2mm(bb.top - (bPt + newH))),
    right: r2(pt2mm(bb.right - (bPt + newW))),
    bottom: r2(pt2mm(bPt - bb.bottom))
  };
  var ovMin = Math.min(Math.abs(overflowMm.left), Math.abs(overflowMm.top),
                       Math.abs(overflowMm.right), Math.abs(overflowMm.bottom));
  var ovMax = Math.max(Math.abs(overflowMm.left), Math.abs(overflowMm.top),
                       Math.abs(overflowMm.right), Math.abs(overflowMm.bottom));
  if (bleedMm > 0) {
    if (ovMin < bleedMm - 0.5) {
      warnings.push("图稿某一侧出血不足：最小 " + r2(ovMin) + "mm，要求 " + bleedMm + "mm");
    }
    if (ovMax > bleedMm + 0.5) {
      warnings.push("图稿超出出血边 " + r2(ovMax) + "mm（要求 ≤ " + bleedMm + "mm）");
    }
  } else if (ovMax > 0.5) {
    warnings.push("图稿超出画板 " + r2(ovMax) + "mm（当前出血设置为 0）");
  }

  var wMm = r2(pt2mm(bb.right - bb.left));
  var hMm = r2(pt2mm(bb.top - bb.bottom));
  if (!closeEnough(wMm, frameW, 0.5) || !closeEnough(hMm, frameH, 0.5)) {
    warnings.push("图稿可见范围 " + wMm + "×" + hMm + "mm 与含出血页面 " + frameW + "×" + frameH + "mm 不等");
  }

  /* 结构统计 */
  var filledCmyk = 0, filledRgb = 0, filledOther = 0, filled = 0, stroked = 0, itemTotal = 0;
  var its = doc.pageItems;
  for (var ci = 0; ci < its.length; ci++) {
    itemTotal++;
    try {
      if (its[ci].filled) {
        filled++;
        var ft = String(its[ci].fillColor.typename);
        if (ft === "CMYKColor") filledCmyk++;
        else if (ft === "RGBColor") filledRgb++;
        else filledOther++;
      }
      if (its[ci].stroked) stroked++;
    } catch (e3) {}
  }

  /* 直接存到最终路径；先复制旧文件做备份，失败就还原。
     不用 rename 做备份：ExtendScript 的 File.rename 会改写 File 对象自身，
     后续 saveAs 会因此算错扩展名（实测产出 .ai.ai）。 */
  var saveOpts = new IllustratorSaveOptions();
  saveOpts.pdfCompatible = true;
  /* 默认值是 false。不嵌入配置文件的话，这个 .ai 换台机器重新打开时
     会按对方的 CMYK 工作空间解释，FOGRA39 的数值就失去依据。 */
  try { saveOpts.embedICCProfile = true; } catch (eICC) { warnings.push("IllustratorSaveOptions 不支持 embedICCProfile，ICC 未嵌入"); }

  var backupPath = outPath + ".prev";
  var backup = new File(backupPath);
  var finalFile = new File(outPath);

  /* 上次运行如果死在"备份完成、保存失败"之间，这里会留下 .prev。
     但它不一定比当前文件新：可能上次其实存成功了、只是没删掉备份。
     只有备份更新时才回滚，否则删掉陈旧备份，避免把较新的成品退回去。 */
  if (backup.exists) {
    var backupTime = 0, currentTime = 0;
    try { backupTime = backup.modified.getTime(); } catch (eB) {}
    try { if (finalFile.exists) currentTime = finalFile.modified.getTime(); } catch (eC) {}
    var shouldRestore = !finalFile.exists || backupTime > currentTime;
    if (shouldRestore) {
      if (finalFile.exists && !finalFile.remove()) {
        throw new Error("发现比成品更新的备份 " + backupPath + "，但无法移除损坏的成品，已停止");
      }
      if (!backup.copy(outPath)) {
        throw new Error("发现比成品更新的备份 " + backupPath + "，但恢复失败，已停止");
      }
      warnings.push("已从上次遗留的备份恢复 " + outPath + "（备份时间晚于成品）");
    }
    backup.remove();
    finalFile = new File(outPath);
  }

  var hadPrevious = finalFile.exists;
  if (hadPrevious && !finalFile.copy(backupPath)) {
    throw new Error("无法备份已有的 " + outPath + "，为避免覆盖已停止");
  }

  try {
    doc.saveAs(new File(outPath), saveOpts);
  } catch (saveErr) {
    if (hadPrevious) {
      var restore = new File(backupPath);
      if (restore.exists && restore.copy(outPath)) restore.remove();
    }
    throw saveErr;
  }

  if (backup.exists) backup.remove();

  doc.close(SaveOptions.DONOTSAVECHANGES);
  doc = null;

  finish(true, {
    output: job.output.ai,
    input: inputRel,
    colorMode: "CMYK (converted by Ghostscript with " + spec.print.iccName + ")",
    trimMm: { w: r2(wmm), h: r2(hmm) },
    artboardMm: { w: r2(pt2mm(newW)), h: r2(pt2mm(newH)) },
    frameMm: { w: r2(frameW), h: r2(frameH) },
    bleedMm: r2(bleedMm),
    scalePercent: r2(factorPct),
    scaleAppliedInPdf: alreadyFinal,
    artworkMm: { w: wMm, h: hMm },
    overflowMm: overflowMm,
    itemCount: itemTotal,
    filled: filled,
    filledCmyk: filledCmyk,
    filledRgb: filledRgb,
    filledOther: filledOther,
    strokedItems: stroked,
    textFrames: 0
  });
} catch (e) {
  finish(false, { error: String(e && e.message ? e.message : e) });
} finally {
  try { if (doc) doc.close(SaveOptions.DONOTSAVECHANGES); } catch (e5) {}
  try { if (prevInteraction !== null) app.userInteractionLevel = prevInteraction; } catch (e6) {}
}
