#target illustrator

/*
 * 03-qa.jsx
 * Open jobs/<id>/<job-id>.ai, run document level checks and write result.json.
 * The run is read-only and closes without saving.
 *
 * Split of checks:
 *   - This script reports what Illustrator can see: colour mode, colour space, links, artboard, raster display size
 *   - 04-report.py reads pixel counts and ICC from the source PDF, then merges them into qa.json
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
  if (!f.exists) throw new Error("deliverable does not exist: " + aiPath);
  out.deliverable = job.output.ai;
  out.deliverableBytes = f.length;

  /* Open directly as normal. If Illustrator refuses the path because of state left by a crash,
     check a copy in work/ instead. QA never writes to the original. */
  var doc = null;
  var qaCopyPath = null;
  out.warnings = [];
  /* Leave the user's open document alone: app.open may reuse it */
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
        throw new Error("the deliverable is already open in your window; close it before running QA, so unsaved content stays intact");
      }
    }
    doc = openedDoc;
  } else {
    /* Fall back to the work/ copy when the original path fails to open. This is a known Illustrator path cache issue. */
    var workDir = new Folder(JOB_DIR + "/work");
    if (!workDir.exists) workDir.create();
    var copyPath = JOB_DIR + "/work/qa-copy.ai";
    var copyFile = new File(copyPath);
    if (copyFile.exists) copyFile.remove();
    if (!f.copy(copyPath)) throw openError;
    qaCopyPath = copyPath;
    doc = app.open(new File(copyPath));
    out.warnings.push("original path failed to open (" + String(openError && openError.message ? openError.message : openError) +
      "). QA uses the copy work/qa-copy.ai. Restart Illustrator and confirm the original opens before delivery.");
  }
  if (!doc) throw new Error("cannot open print.ai");

  /* Colour mode and the profile the document uses */
  out.docColorSpace = String(doc.documentColorSpace);
  out.isCMYK = doc.documentColorSpace === DocumentColorSpace.CMYK;
  try { out.colorProfileName = String(doc.colorProfileName); } catch (eProf) {}
  try { out.layerCount = doc.layers.length; } catch (eLayer) {}
  try { out.itemCount = doc.pageItems.length; } catch (eItems) {}

  /* Artboard: the .ai artboard is the trim rectangle; artwork extends 3 mm for bleed */
  var art = doc.artboards[0].artboardRect;
  out.artboardMm = { w: r2(pt2mm(art[2] - art[0])), h: r2(pt2mm(art[1] - art[3])) };
  out.artboardCount = doc.artboards.length;

  /* Links: must be 0. A linked item leaves a missing image in the deliverable. */
  out.linkedItems = doc.placedItems.length;

  /* Text: this flow exports Figma text outlined; the count must be 0 */
  out.textFrames = doc.textFrames.length;
  out.fonts = [];
  for (var it = 0; it < doc.textFrames.length; it++) {
    try { out.fonts.push(String(doc.textFrames[it].textRange.characterAttributes.textFont.name)); } catch (e2) {}
  }

  /* Spot colours. Registration is built in and stays out of the list.
     The zh_TW UI shows Registration as the plate registration colour, so compare colorType, not the name. */
  var spots = [];
  for (var s = 0; s < doc.spots.length; s++) {
    var sp = doc.spots[s];
    var ct = "";
    try { ct = String(sp.colorType); } catch (e1) {}
    if (ct.indexOf("REGISTRATION") !== -1) continue;
    spots.push(String(sp.name));
  }
  out.spotColors = spots;

  /* Colour space counts come from filled/fillColor.
     In our tests fills stays an empty array for PDF-imported artwork; fillColor carries the real fill. */
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

  /* Rasters: display size and internal colour space. Python reads pixel counts from the PDF and merges them. */
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

  /* Overflow check: keep all artwork inside the artboard */
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
