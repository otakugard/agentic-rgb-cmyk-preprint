#target illustrator

/*
 * 07b-export-pdf.jsx
 * 从已批准的 <job-id>.ai 导出候选交付 PDF。只做 PDF 导出，不改 .ai。
 *
 * 参数来源：job.pdfExport（fileName / contract / layout）与 config/print-spec.json。
 * 导出后由 07c-finalize-pdf.py 统一页面框、写 /DefaultCMYK ICC 并做哈希绑定。
 */

var RESULT_PATH = JOB_DIR + "/result.json";
var EXPORT_PATH = JOB_DIR + "/work/export-result.json";
var warnings = [];
var result = { ok: false, script: "07b-export-pdf", jobId: null, warnings: warnings };
var doc = null;
var prevInteraction = null;

function esc(s) {
  s = String(s);
  s = s.replace(/\\/g, "\\\\").replace(/"/g, '\\"');
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
function readText(path) {
  var f = new File(path);
  if (!f.exists) throw new Error("missing: " + path);
  f.encoding = "UTF-8";
  f.open("r");
  var s = f.read();
  f.close();
  return s;
}
function writeText(path, s) {
  var f = new File(path);
  f.encoding = "UTF-8";
  if (!f.open("w")) throw new Error("cannot write: " + path);
  f.write(s);
  f.close();
}
function mm2pt(mm) { return mm * 72 / 25.4; }

try {
  var job = eval("(" + readText(JOB_DIR + "/job.json") + ")");
  result.jobId = job.jobId || null;
  var cfg = job.pdfExport || {};
  var fileName = cfg.fileName || "";
  if (!/^[A-Za-z0-9._-]+\.pdf$/.test(fileName)) {
    throw new Error("job.pdfExport.fileName must be a single .pdf filename");
  }
  result.fileName = fileName;
  result.contract = cfg.contract || "ordinary-pdf-1.6";
  result.layout = cfg.layout || "marks";

  var offsetMm = Number(cfg.offsetMm || 3);
  var bleedMm = Number(job.bleedMm || 3);
  if (!isFinite(offsetMm) || offsetMm < 0) {
    throw new Error("pdfExport.offsetMm must be >= 0");
  }

  var input = JOB_DIR + "/" + job.output.ai;
  var raw = JOB_DIR + "/work/" + fileName.replace(/\.pdf$/, ".raw.pdf");
  var oldRaw = new File(raw);
  if (oldRaw.exists) oldRaw.remove();

  prevInteraction = app.userInteractionLevel;
  app.userInteractionLevel = UserInteractionLevel.DONTDISPLAYALERTS;

  /* 不碰用户已打开的同一份文档 */
  var preOpen = [];
  for (var d = 0; d < app.documents.length; d++) {
    try { preOpen.push(String(app.documents[d].fullName)); } catch (e0) {}
  }

  doc = app.open(new File(input));
  var opened = "";
  try { opened = String(doc.fullName); } catch (e1) {}
  for (var p = 0; p < preOpen.length; p++) {
    if (opened !== "" && preOpen[p] === opened) {
      doc = null;
      throw new Error("the .ai is already open in your window; close it before exporting");
    }
  }

  try { doc.rulerUnits = RulerUnits.Millimeters; } catch (eU) {
    warnings.push("doc.rulerUnits assignment failed: " + String(eU.message || eU));
  }
  result.rulerUnits = String(doc.rulerUnits);

  /* 只导出原 .ai 的 297×210 画板 + 印刷标记。出血由 07c 从
     work/cmyk-final.pdf 合成；这样标记围绕成品 297×210，不会围绕 303
     的出血框，也不会因改画板触发 MRAP。 */
  result.method = "ai-artboard-marks";
  result.exportArtboardMm = { w: 297, h: 210 };

  var o = new PDFSaveOptions();
  o.compatibility = PDFCompatibility.ACROBAT7;
  o.pDFXStandard = PDFXStandard.PDFXNONE;
  o.artboardRange = "1";
  o.preserveEditability = true;
  o.generateThumbnails = true;
  o.acrobatLayers = true;
  o.optimization = false;
  o.viewAfterSaving = false;

  o.colorCompression = CompressionQuality.ZIP8BIT;
  o.grayscaleCompression = CompressionQuality.ZIP8BIT;
  o.monochromeCompression = MonochromeCompression.MONOZIP;
  o.colorDownsamplingMethod = DownsampleMethod.NODOWNSAMPLE;
  o.grayscaleDownsamplingMethod = DownsampleMethod.NODOWNSAMPLE;
  o.monochromeDownsamplingMethod = DownsampleMethod.NODOWNSAMPLE;
  o.colorDownsampling = 0;
  o.grayscaleDownsampling = 0;
  o.monochromeDownsampling = 0;
  o.compressArt = true;

  /* 07c 负责绘制日式双线角线与十字线，这里只导出图稿。 */
  o.trimMarks = false;
  o.registrationMarks = false;
  o.colorBars = false;
  o.pageInformation = false;
  o.pageMarksType = PageMarksTypes.Roman;
  o.trimMarkWeight = PDFTrimMarkWeight.TRIMMARKWEIGHT025;
  o.offset = 0;

  o.bleedLink = false;
  o.bleedOffsetRect = [0, 0, 0, 0];

  /* COLORCONVERSIONTODEST / REPURPOSE 在改过画板后都可能 MRAP。
     图稿已是 FOGRA39 CMYK，导出不做颜色转换；ICC 由 07c 以
     /DefaultCMYK 写入，数值不变。 */
  o.colorConversionID = ColorConversion.None;
  o.colorDestinationID = ColorDestination.None;
  o.colorProfileID = ColorProfile.None;

  o.requireDocumentPassword = false;
  o.requirePermissionPassword = false;

  doc.saveAs(new File(raw), o);
  result.rawPdf = "work/" + fileName.replace(/\.pdf$/, ".raw.pdf");
  result.rawBytes = (new File(raw)).exists ? (new File(raw)).length : 0;
  result.offsetMm = offsetMm;
  result.bleedMm = bleedMm;
  result.options = {
    compatibility: String(o.compatibility),
    pDFXStandard: String(o.pDFXStandard),
    colorConversionID: String(o.colorConversionID),
    colorDestinationID: String(o.colorDestinationID),
    colorProfileID: String(o.colorProfileID),
    trimMarks: o.trimMarks,
    registrationMarks: o.registrationMarks,
    colorBars: o.colorBars,
    pageInformation: o.pageInformation,
    trimMarkWeight: String(o.trimMarkWeight),
    offsetPt: o.offset,
    bleedOffsetPt: Number(o.offset) || 0,
    colorCompression: String(o.colorCompression),
    monochromeCompression: String(o.monochromeCompression)
  };

  doc.close(SaveOptions.DONOTSAVECHANGES);
  doc = null;
  result.ok = result.rawBytes > 0;
} catch (e) {
  result.error = String(e && e.message ? e.message : e);
} finally {
  try { if (doc) doc.close(SaveOptions.DONOTSAVECHANGES); } catch (e5) {}
  try { if (prevInteraction !== null) app.userInteractionLevel = prevInteraction; } catch (e6) {}
}

try {
  var tf = new File(JOB_DIR + "/.run-token");
  if (tf.exists) {
    tf.encoding = "UTF-8"; tf.open("r");
    result.runToken = tf.read().replace(/[\r\n]/g, "");
    tf.close();
  }
} catch (e7) {}
writeText(RESULT_PATH, jsonStr(result));
writeText(EXPORT_PATH, jsonStr(result));
