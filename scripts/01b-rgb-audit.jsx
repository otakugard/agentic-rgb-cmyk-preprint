#target illustrator

/*
 * 01b-rgb-audit.jsx
 * 只读打开 source/ 里的 Figma PDF，记录 Illustrator 实际看到的颜色、字体、
 * 位图与画板，并导出 150 dpi TIFF 供 Gate 1 对照。
 *
 * 不保存、不覆盖 source/；关闭时不保存。所有输出写 work/。
 */

var RESULT_PATH = JOB_DIR + "/result.json";
var AUDIT_PATH = JOB_DIR + "/work/rgb-audit.json";
var TIFF_PATH = JOB_DIR + "/work/rgb-ai.tif";

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

var out = {
  ok: false, script: "01b-rgb-audit", jobId: null, exported: false,
  warnings: [], itemTypes: {}, colorUsage: { rgb: 0, cmyk: 0, gray: 0, spot: 0, other: 0 },
  rgbHistogram: {}, fonts: [], rasters: [], placedItems: 0
};

try {
  var job = eval("(" + readText(JOB_DIR + "/job.json") + ")");
  out.jobId = job.jobId || null;
  var src = JOB_DIR + "/" + job.source.pdf;
  out.input = job.source.pdf;
  /* 01-prepare 会把 Figma 的矩形 alpha 裁切蒙版改写成白色填充的 alpha 蒙版；
     Illustrator 才能正确看到版面。源文件不动，这里只读该副本。 */
  var cleaned = new File(JOB_DIR + "/work/source-white.pdf");
  if (job.maskPolicy === "whiten-alpha") {
    var prepPath = JOB_DIR + "/prepare.json";
    var prepFile = new File(prepPath);
    if (!prepFile.exists) throw new Error("maskPolicy is set but prepare.json is missing; rerun 01-prepare.sh");
    var prep = eval("(" + readText(prepPath) + ")");
    var mr = prep.maskReport || {};
    var srcSha = (job.source && job.source.sha256) || "";
    var reportOk = mr.ok === true && mr.mode === "rewrite"
      && mr.inputSha256 === srcSha
      && prep.convertedFrom === "work/source-white.pdf"
      && cleaned.exists;
    if (!reportOk) {
      throw new Error("maskPolicy is set but the mask report is missing, stale, or not ok; rerun 01-prepare.sh");
    }
    src = JOB_DIR + "/work/source-white.pdf";
    out.input = "work/source-white.pdf";
    out.warningCleanedSource = true;
    out.warnings.push("RGB 审计使用 work/source-white.pdf（alpha 裁切蒙版已改写为白色填充的 alpha 蒙版），源文件未修改");
  }
  var f = new File(src);
  if (!f.exists) throw new Error("source PDF missing: " + src);

  app.userInteractionLevel = UserInteractionLevel.DONTDISPLAYALERTS;

  /* 不碰用户已打开的同一份文档 */
  var preOpen = [];
  for (var d = 0; d < app.documents.length; d++) {
    try { preOpen.push(String(app.documents[d].fullName)); } catch (e0) {}
  }

  var doc = app.open(f);
  if (!doc) throw new Error("open failed: " + src);
  var opened = "";
  try { opened = String(doc.fullName); } catch (e1) {}
  for (var p = 0; p < preOpen.length; p++) {
    if (opened !== "" && preOpen[p] === opened) {
      doc = null;
      throw new Error("source PDF is already open in your window; close it before running audit");
    }
  }

  try { out.documentColorSpace = String(doc.documentColorSpace); } catch (e2) {}
  try { out.colorProfileName = String(doc.colorProfileName); } catch (e3) {}
  var art = doc.artboards[0].artboardRect;
  out.artboardMm = { w: r2(pt2mm(art[2] - art[0])), h: r2(pt2mm(art[1] - art[3])) };
  out.artboardCount = doc.artboards.length;
  out.layers = doc.layers.length;
  out.placedItems = doc.placedItems.length;
  out.textFrames = doc.textFrames.length;

  for (var t = 0; t < doc.textFrames.length; t++) {
    try { out.fonts.push(String(doc.textFrames[t].textRange.characterAttributes.textFont.name)); } catch (e4) {}
  }
  var spots = [];
  for (var s = 0; s < doc.spots.length; s++) {
    var ct = "";
    try { ct = String(doc.spots[s].colorType); } catch (e5) {}
    if (ct.indexOf("REGISTRATION") === -1) spots.push(String(doc.spots[s].name));
  }
  out.spotColors = spots;

  function rgbHex(c) {
    var r = Math.max(0, Math.min(255, Math.round(c.red)));
    var g = Math.max(0, Math.min(255, Math.round(c.green)));
    var b = Math.max(0, Math.min(255, Math.round(c.blue)));
    var h = "#" + ("0" + r.toString(16)).slice(-2) + ("0" + g.toString(16)).slice(-2) + ("0" + b.toString(16)).slice(-2);
    return h.toUpperCase();
  }

  var items = doc.pageItems;
  var scanErrors = 0;
  for (var i = 0; i < items.length; i++) {
    var it = items[i];
    var tn = String(it.typename);
    out.itemTypes[tn] = (out.itemTypes[tn] || 0) + 1;
    try {
      if (it.filled) {
        var c = it.fillColor;
        var ct2 = String(c.typename);
        if (ct2 === "RGBColor") {
          out.colorUsage.rgb++;
          var hx = rgbHex(c);
          out.rgbHistogram[hx] = (out.rgbHistogram[hx] || 0) + 1;
        } else if (ct2 === "CMYKColor") out.colorUsage.cmyk++;
        else if (ct2 === "GrayColor") out.colorUsage.gray++;
        else if (ct2 === "SpotColor") out.colorUsage.spot++;
        else out.colorUsage.other++;
      }
      if (it.stroked) {
        var sc = it.strokeColor;
        var st = String(sc.typename);
        if (st === "RGBColor") {
          out.colorUsage.rgb++;
          var sh = rgbHex(sc);
          out.rgbHistogram[sh] = (out.rgbHistogram[sh] || 0) + 1;
        } else if (st === "CMYKColor") out.colorUsage.cmyk++;
        else if (st === "GrayColor") out.colorUsage.gray++;
        else if (st === "SpotColor") out.colorUsage.spot++;
        else out.colorUsage.other++;
      }
    } catch (e6) { scanErrors++; }
  }
  out.scanErrors = scanErrors;

  /* pathItems 覆盖 group / compound path 内部；pageItems 的顶层统计会漏掉
     转曲文字这类嵌套填充。用单独直方图做 Gate 1 的色板集合比对。 */
  var pathHist = {};
  var pathFilled = 0;
  for (var pp = 0; pp < doc.pathItems.length; pp++) {
    var pth = doc.pathItems[pp];
    try {
      if (pth.filled && String(pth.fillColor.typename) === "RGBColor") {
        pathFilled++;
        var ph = rgbHex(pth.fillColor);
        pathHist[ph] = (pathHist[ph] || 0) + 1;
      }
    } catch (ePath) {}
  }
  out.pathRgbHistogram = pathHist;
  out.pathFilled = pathFilled;

  var rasters = [];
  for (var ri = 0; ri < doc.rasterItems.length; ri++) {
    var r = doc.rasterItems[ri];
    var linked = false;
    try { linked = !!r.file; } catch (e7) {}
    var cs = "unknown";
    try { cs = String(r.imageColorSpace); } catch (e8) {}
    rasters.push({ wMm: r2(pt2mm(r.width)), hMm: r2(pt2mm(r.height)), imageColorSpace: cs, embedded: !linked });
  }
  out.rasters = rasters;

  /* 150 dpi、无压缩 TIFF；Gate 1 用它与 Ghostscript 的源渲染对照 */
  try {
    var opts = new ExportOptionsTIFF();
    opts.resolution = 150;
    opts.imageColorSpace = ImageColorSpace.RGB;
    opts.lZWCompression = false;
    opts.antiAliasing = AntiAliasingMethod.ARTOPTIMIZED;
    opts.embedICCProfile = false;
    var tif = new File(TIFF_PATH);
    if (tif.exists) tif.remove();
    doc.exportFile(tif, ExportType.TIFF, opts);
    out.exported = tif.exists;
    if (!out.exported) out.warnings.push("TIFF export did not create a file");
  } catch (e9) {
    out.warnings.push("TIFF export failed: " + String(e9 && e9.message ? e9.message : e9));
  }

  doc.close(SaveOptions.DONOTSAVECHANGES);
  doc = null;
  out.ok = true;
} catch (e) {
  out.error = String(e && e.message ? e.message : e);
  try { if (typeof doc !== "undefined" && doc) doc.close(SaveOptions.DONOTSAVECHANGES); } catch (ignore) {}
}

try {
  var tf = new File(JOB_DIR + "/.run-token");
  if (tf.exists) {
    tf.encoding = "UTF-8"; tf.open("r");
    out.runToken = tf.read().replace(/[\r\n]/g, "");
    tf.close();
  }
} catch (e10) {}

writeText(RESULT_PATH, jsonStr(out));
writeText(AUDIT_PATH, jsonStr(out));
