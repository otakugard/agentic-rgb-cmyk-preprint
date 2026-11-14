#target illustrator

/*
 * 03-qa.jsx
 * 打开 jobs/<id>/<job-id>.ai，做文档级自检，写 result.json。
 * 不做任何修改，只读；关闭时不保存。
 *
 * 判定分工：
 *   - 这里只负责"Illustrator 侧能看到的事实"（色彩模式、颜色空间、链接、画板、位图显示尺寸）
 *   - 源 PDF 的像素数、ICC 由 04-report.py 读，最后合并成 qa.json
 */

var RESULT_PATH = JOB_DIR + "/result.json";
var QA_RESULT_PATH = JOB_DIR + "/work/qa-result.json";
var PA_ROOT = SPEC_PATH.replace(/\/config\/print-spec\.json$/, "");

function writeText(path, s) {
  var f = new File(path);
  f.encoding = "UTF-8";
  if (!f.open("w")) throw new Error("cannot write: " + path);
  f.write(s);
  f.close();
}
function readText(path) {
  var f = new File(path);
  if (!f.exists) throw new Error("missing: " + path);
  f.encoding = "UTF-8";
  f.open("r");
  var s = f.read();
  f.close();
  return s;
}
function esc(s) {
  s = String(s).replace(/\\/g, "\\\\").replace(/"/g, '\\"');
  s = s.replace(/\n/g, "\\n").replace(/\r/g, "\\r");
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
function pt2mm(pt) { return pt * 25.4 / 72; }
function r2(v) { return Math.round(v * 100) / 100; }

var out = { ok: false, script: "03-qa", jobId: null };

try {
  var spec = eval("(" + readText(SPEC_PATH) + ")");
  var job = eval("(" + readText(JOB_DIR + "/job.json") + ")");
  out.jobId = job.jobId || null;

  app.userInteractionLevel = UserInteractionLevel.DONTDISPLAYALERTS;

  var aiPath = JOB_DIR + "/" + ((job.output && job.output.ai) ? job.output.ai : ((job.jobId || "job") + ".ai"));
  var f = new File(aiPath);
  if (!f.exists) throw new Error("交付文件不存在: " + aiPath);
  out.deliverable = job.output.ai;
  out.deliverableBytes = f.length;

  /* 正常直接打开。若 Illustrator 因为上次崩溃残留状态拒绝该路径，
     改用 work/ 里的副本做只读检查——QA 本来就不需要原文件可写。 */
  var doc = null;
  var qaCopyPath = null;
  out.warnings = [];
  /* 不碰用户已打开的同一份文档：app.open 可能直接复用 */
  var preOpenNames = [];
  for (var pd = 0; pd < app.documents.length; pd++) {
    try { preOpenNames.push(String(app.documents[pd].fullName)); } catch (ePre) {}
  }
  var openedDoc = null;
  var openError = null;
  try {
    openedDoc = app.open(f);
  } catch (eOpen) {
    openError = eOpen;
  }
  if (openedDoc) {
    var openedName = "";
    try { openedName = String(openedDoc.fullName); } catch (eOpenName) {}
    for (var pn = 0; pn < preOpenNames.length; pn++) {
      if (openedName !== "" && preOpenNames[pn] === openedName) {
        openedDoc = null;
        throw new Error("交付文件已在你打开的窗口里；请先关闭再跑 QA，避免改动未保存内容");
      }
    }
    doc = openedDoc;
  } else {
    /* 原路径打不开时用 work/ 副本兜底；这是已知的 Illustrator 路径缓存问题 */
    var workDir = new Folder(JOB_DIR + "/work");
    if (!workDir.exists) workDir.create();
    var copyPath = JOB_DIR + "/work/qa-copy.ai";
    var copyFile = new File(copyPath);
    if (copyFile.exists) copyFile.remove();
    if (!f.copy(copyPath)) throw openError;
    qaCopyPath = copyPath;
    doc = app.open(new File(copyPath));
    out.warnings.push("原路径无法打开（" + String(openError && openError.message ? openError.message : openError) +
      "），QA 使用副本 work/qa-copy.ai；交付前请重启 Illustrator 再确认原文件可正常打开");
  }
  if (!doc) throw new Error("打开 print.ai 失败");

  /* 色彩模式与文档实际配置文件 */
  out.docColorSpace = String(doc.documentColorSpace);
  out.isCMYK = doc.documentColorSpace === DocumentColorSpace.CMYK;
  try { out.colorProfileName = String(doc.colorProfileName); } catch (eProf) {}
  try { out.layerCount = doc.layers.length; } catch (eLayer) {}
  try { out.itemCount = doc.pageItems.length; } catch (eItems) {}

  /* 画板：AI 的画板是成品矩形，图稿外扩 3 mm 为出血 */
  var art = doc.artboards[0].artboardRect;
  out.artboardMm = { w: r2(pt2mm(art[2] - art[0])), h: r2(pt2mm(art[1] - art[3])) };
  out.artboardCount = doc.artboards.length;

  /* 链接项：必须为 0，否则交付文件会缺图 */
  out.linkedItems = doc.placedItems.length;

  /* 文字：当前流程下 Figma 已转曲，这里应为 0 */
  out.textFrames = doc.textFrames.length;
  out.fonts = [];
  for (var it = 0; it < doc.textFrames.length; it++) {
    try { out.fonts.push(String(doc.textFrames[it].textRange.characterAttributes.textFont.name)); } catch (e2) {}
  }

  /* 专色（排除 Registration，它是内置的，不算问题）
     Registration 在 zh_TW 界面叫「[拼版標示色]」，不能靠英文名判断，要用 colorType */
  var spots = [];
  for (var s = 0; s < doc.spots.length; s++) {
    var sp = doc.spots[s];
    var ct = "";
    try { ct = String(sp.colorType); } catch (e1) {}
    if (ct.indexOf("REGISTRATION") !== -1) continue;
    spots.push(String(sp.name));
  }
  out.spotColors = spots;

  /* 颜色空间统计：用 filled/fillColor 而不是 fills 数组
     实测 fills 对 PDF 导入的图稿恒为空数组，只有 fillColor 才反映真实填充 */
  var counts = { rgb: 0, cmyk: 0, spot: 0, gray: 0, other: 0, filled: 0, stroked: 0 };
  out.cmykHistogram = {};
  out.neutralDark = 0;
  out.pureK = 0;
  out.strokeCount = 0;
  out.strokeMinMm = null;
  out.strokeUnder0_1Mm = 0;
  out.strokeUnder0_25Mm = 0;

  function bump(c) {
    var t = String(c.typename);
    if (t === "RGBColor") counts.rgb++;
    else if (t === "CMYKColor") {
      counts.cmyk++;
      try {
        var cy = Math.round(c.cyan * 10) / 10;
        var ma = Math.round(c.magenta * 10) / 10;
        var ye = Math.round(c.yellow * 10) / 10;
        var bk = Math.round(c.black * 10) / 10;
        var key = cy + "/" + ma + "/" + ye + "/" + bk;
        out.cmykHistogram[key] = (out.cmykHistogram[key] || 0) + 1;
        var mx = Math.max(cy, ma, ye, bk);
        var mn = Math.min(cy, ma, ye, bk);
        if (mx - mn <= 10 && bk >= 40) out.neutralDark++;
        if (cy === 0 && ma === 0 && ye === 0 && bk > 0) out.pureK++;
      } catch (eCmyk) {}
    }
    else if (t === "SpotColor") counts.spot++;
    else if (t === "GrayColor") counts.gray++;
    else counts.other++;
  }

  var items = doc.pageItems;
  var itemTypes = {};
  var scanErrors = 0;
  for (var i = 0; i < items.length; i++) {
    var it = items[i];
    var tn = String(it.typename);
    itemTypes[tn] = (itemTypes[tn] || 0) + 1;
    try {
      if (it.filled) {
        counts.filled++;
        bump(it.fillColor);
      }
      if (it.stroked) {
        counts.stroked++;
        bump(it.strokeColor);
        out.strokeCount++;
        try {
          var sw = pt2mm(it.strokeWidth);
          if (out.strokeMinMm === null || sw < out.strokeMinMm) out.strokeMinMm = r2(sw);
          if (sw < 0.1) out.strokeUnder0_1Mm++;
          if (sw < 0.25) out.strokeUnder0_25Mm++;
        } catch (eStroke) {}
      }
    } catch (e5) { scanErrors++; }
  }
  out.itemTypes = itemTypes;
  out.scanErrors = scanErrors;
  out.colorUsage = counts;

  /* 位图：显示尺寸 + 内部色彩空间，像素数由 python 侧读 PDF 后合并 */
  var rasters = [];
  for (var ri = 0; ri < doc.rasterItems.length; ri++) {
    var r = doc.rasterItems[ri];
    var isLinked = false;
    try { isLinked = !!r.file; } catch (e6) { isLinked = false; }
    var imgCs = "unknown";
    try { imgCs = String(r.imageColorSpace); } catch (e7) {}
    rasters.push({
      wPt: r2(r.width),
      hPt: r2(r.height),
      wMm: r2(pt2mm(r.width)),
      hMm: r2(pt2mm(r.height)),
      aspect: Math.round((r.width / r.height) * 1000) / 1000,
      imageColorSpace: imgCs,
      embedded: !isLinked
    });
  }
  out.rasters = rasters;
  var linkedRasters = [];
  for (var lr = 0; lr < rasters.length; lr++) {
    if (!rasters[lr].embedded) linkedRasters.push(rasters[lr].wMm + "×" + rasters[lr].hMm + "mm");
  }
  out.linkedRasters = linkedRasters;

  /* 溢出检查：所有图稿是否落在画板内 */
  var l = Infinity, t = -Infinity, rr = -Infinity, b = Infinity;
  for (var k = 0; k < items.length; k++) {
    var gb = items[k].geometricBounds;
    if (gb[0] < l) l = gb[0];
    if (gb[1] > t) t = gb[1];
    if (gb[2] > rr) rr = gb[2];
    if (gb[3] < b) b = gb[3];
  }
  out.artworkMm = { w: r2(pt2mm(rr - l)), h: r2(pt2mm(t - b)) };
  out.overflowMm = {
    left: r2(pt2mm(art[0] - l)),
    top: r2(pt2mm(t - art[1])),
    right: r2(pt2mm(rr - art[2])),
    bottom: r2(pt2mm(art[3] - b))
  };

  doc.close(SaveOptions.DONOTSAVECHANGES);
  doc = null;
  if (qaCopyPath) {
    try { var qc = new File(qaCopyPath); if (qc.exists) qc.remove(); } catch (e9) {}
  }
  out.ok = true;

  try {
    var tf = new File(JOB_DIR + "/.run-token");
    if (tf.exists) {
      tf.encoding = "UTF-8"; tf.open("r");
      out.runToken = tf.read().replace(/[\r\n]/g, "");
      tf.close();
    }
  } catch (e8) {}

  writeText(RESULT_PATH, jsonStr(out));
  writeText(QA_RESULT_PATH, jsonStr(out));
} catch (e) {
  out.error = String(e && e.message ? e.message : e);
  writeText(RESULT_PATH, jsonStr(out));
  try { writeText(QA_RESULT_PATH, jsonStr(out)); } catch (eQa) {}
  try {
    if (typeof doc !== "undefined" && doc) doc.close(SaveOptions.DONOTSAVECHANGES);
  } catch (ignore) {}
}
