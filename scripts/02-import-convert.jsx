#target illustrator

/*
 * 02-import-convert.jsx
 * Turns work/cmyk.pdf (already converted to Coated FOGRA39 by 01-prepare.sh) into <job-id>.ai.
 *
 * Precondition: 01-prepare.sh has run. This script does no colour conversion.
 * Ghostscript fixes the colour. This script handles size and structure only.
 *
 * Route (measured):
 *   1. app.open(work/cmyk.pdf) uses native PDF import and returns editable vector artwork
 *   2. Scale everything to the real trim size (Figma 1px=1pt, factor 0.75)
 *   3. Align the artboard and save print.ai
 *
 * Place + embed turns the whole page into one NonNativeItem in our tests; the colour stays RGB.
 * doc-color-cmyk follows the current Illustrator working CMYK space; the result can leave FOGRA39.
 *
 * Input (injected by run.sh): JOB_DIR, SPEC_PATH
 * Output: <JOB_DIR>/<job-id>.ai, <JOB_DIR>/result.json
 */

var RESULT_PATH = JOB_DIR + "/result.json";
var warnings = [];
var result = { ok: false, script: "02-import-convert", jobId: null, warnings: warnings };
var doc = null;
var prevInteraction = null;

/* ---------- Helpers ---------- */

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

/* The output path stays inside this job directory and outside source/.
   Normalise the path first: a form like `./source/x.ai` bypasses a naive prefix check. */
function normalizeRel(rel) {
  var parts = String(rel).split("/");
  var out = [];
  for (var i = 0; i < parts.length; i++) {
    var p = parts[i];
    if (p === "" || p === ".") continue;
    if (p === "..") throw new Error("output.ai may not contain ..: " + rel);
    out.push(p);
  }
  return out.join("/");
}

function safeOutput(rel) {
  if (!rel) throw new Error("job.json has no output.ai");
  if (String(rel).charAt(0) === "/") throw new Error("output.ai must be a relative path: " + rel);
  var norm = normalizeRel(rel);
  if (norm.indexOf("source/") === 0) throw new Error("output.ai may not go into source/: " + rel);
  if (norm === "") throw new Error("output.ai is empty after normalisation: " + rel);
  return JOB_DIR + "/" + norm;
}

/* ---------- Main flow ---------- */

try {
  var spec = readJSON(SPEC_PATH);
  var job = readJSON(JOB_DIR + "/job.json");
  result.jobId = job.jobId || null;

  /* Verify the output path before touching Illustrator */
  var outPath = safeOutput(job.output && job.output.ai);

  /* Trim size: when job gives preset and w/h, w/h wins (same as 04-report) */
  var wmm, hmm;
  if (job.trim && job.trim.w && job.trim.h) {
    wmm = job.trim.w; hmm = job.trim.h;
  } else if (job.trim && job.trim.preset && spec.trimPresetsMm[job.trim.preset]) {
    var preset = spec.trimPresetsMm[job.trim.preset];
    wmm = preset[0]; hmm = preset[1];
  } else {
    throw new Error("cannot parse trim in job.json (need preset or w/h)");
  }

  /* The artboard carries the bleed: job.bleedMm wins, then spec.print.bleedMm.
     Source PDF page = trim + 2× bleed. The artboard sets the trim; artwork outside it is bleed. */
  var bleedMm = (job.bleedMm !== undefined && job.bleedMm !== null)
    ? Number(job.bleedMm) : Number(spec.print.bleedMm || 0);
  if (!isFinite(bleedMm) || bleedMm < 0) bleedMm = 0;
  var frameW = wmm + 2 * bleedMm;
  var frameH = hmm + 2 * bleedMm;

  /* Input: prefer work/cmyk.pdf. A missing file throws; the RGB original stays untouched */
  var prepPath = JOB_DIR + "/prepare.json";
  var inputRel = "work/cmyk.pdf";
  /* Optional: when 01-prepare already scaled the geometry to trim size, import that file
     and skip another group/resize on multi panel clip groups inside Illustrator. */
  if (job.scaleInPdf && new File(JOB_DIR + "/work/cmyk-final.pdf").exists) {
    inputRel = "work/cmyk-final.pdf";
  }
  if (!(new File(JOB_DIR + "/" + inputRel)).exists) {
    throw new Error("missing work/cmyk.pdf; run scripts/01-prepare.sh first " + (job.jobId || jobId));
  }
  if (new File(prepPath).exists) {
    var prep = readJSON(prepPath);
    if (prep.deviceRGB && prep.deviceRGB > 0) {
      warnings.push("prepare output still has DeviceRGB at " + prep.deviceRGB + " places");
    }
  }

  prevInteraction = app.userInteractionLevel;
  app.userInteractionLevel = UserInteractionLevel.DONTDISPLAYALERTS;

  /* Record the document list before opening. If app.open reuses a document the user has open,
     this script would unlock, scale and save it, which changes the user's work. Stop here. */
  var preOpenNames = [];
  for (var di = 0; di < app.documents.length; di++) {
    try { preOpenNames.push(String(app.documents[di].fullName)); } catch (eD) {}
  }

  doc = app.open(new File(JOB_DIR + "/" + inputRel));
  if (!doc) throw new Error("cannot open work/cmyk.pdf");

  var openedName = "";
  try { openedName = String(doc.fullName); } catch (eN) {}
  for (var pi = 0; pi < preOpenNames.length; pi++) {
    if (openedName !== "" && preOpenNames[pi] === openedName) {
      doc = null; // keep the user's own document open
      throw new Error("work/cmyk.pdf is already open in your window. Close it before running, so the script leaves unsaved content alone.");
    }
  }

  doc.rulerUnits = RulerUnits.Millimeters;

  if (doc.documentColorSpace !== DocumentColorSpace.CMYK) {
    throw new Error("not CMYK after open (current " + String(doc.documentColorSpace) +
      "). 01-prepare converted the wrong way, or Ghostscript used the wrong ICC.");
  }

  /* Multiple layers stop the run: regrouping across layers changes stacking order */
  var usableLayers = [];
  for (var li = 0; li < doc.layers.length; li++) {
    if (doc.layers[li].pageItems.length > 0) usableLayers.push(doc.layers[li]);
  }
  if (usableLayers.length > 1) {
    throw new Error("source file has " + usableLayers.length + " layers; this flow takes one layer. " +
      "Merge the layers in Illustrator first, or change the flow (regrouping across layers changes stacking order).");
  }
  if (usableLayers.length === 0) throw new Error("source PDF has no artwork");

  for (var ui = 0; ui < doc.pageItems.length; ui++) {
    try { doc.pageItems[ui].locked = false; } catch (e1) {}
    try { doc.pageItems[ui].hidden = false; } catch (e2) {}
  }

  /* Orientation: a 90 degree swap is allowed. Non-uniform scaling stops the run with an error. */
  var ab = doc.artboards[0].artboardRect;
  var srcW = ab[2] - ab[0];
  var srcH = ab[1] - ab[3];
  var srcAspect = srcW / srcH;
  var targetAspect = frameW / frameH;
  var tol = 0.005;

  if (closeEnough(srcAspect, targetAspect, tol)) {
    // match
  } else if (closeEnough(srcAspect, 1 / targetAspect, tol)) {
    var swap = wmm; wmm = hmm; hmm = swap;
    var swapB = frameW; frameW = frameH; frameH = swapB;
    targetAspect = frameW / frameH;
    warnings.push("source is rotated 90 degrees; trim size follows the content: " + wmm + "×" + hmm + "mm");
  } else {
    throw new Error("aspect ratio mismatch: source " + r2(srcAspect) + ", target " + r2(targetAspect) +
      " (with bleed " + frameW + "×" + frameH + "mm, trim " + wmm + "×" + hmm + "mm). Non-uniform scaling stays off.");
  }

  var targetWpt = mm2pt(frameW);
  var targetHpt = mm2pt(frameH);
  var factorPct = targetWpt / srcW * 100;

  var alreadyFinal = closeEnough(factorPct, 100, 0.1);
  var layer = usableLayers[0];
  var s = factorPct / 100;

  if (!alreadyFinal) {
    /* Contract check for Figma 1px=1pt: any deviation warns */
    var expected = spec.figma.pxToPt * 100;
    if (!closeEnough(factorPct, expected, 0.5)) {
      warnings.push("scale factor " + r2(factorPct) + "% differs from spec.figma.pxToPt(" + expected +
        "%), so the source PDF size breaks the Figma 1px=1pt contract");
    }

    /* Group the whole layer, then scale: a per item resize pivots on each item centre and breaks the layout */
    var snapshot = [];
    for (var si = 0; si < layer.pageItems.length; si++) snapshot.push(layer.pageItems[si]);

    var preUnion = unionBounds(doc.pageItems);
    var grp = layer.groupItems.add();
    for (var mi = 0; mi < snapshot.length; mi++) {
      snapshot[mi].move(grp, ElementPlacement.PLACEATEND);
    }
    /* Argument 7 is the stroke width scale percent and needs a number. Passing true reads as 1%. */
    grp.resize(factorPct, factorPct, true, true, true, true, factorPct);

    try {
      doc.selection = null;
      grp.selected = true;
      app.executeMenuCommand("ungroup");
      doc.selection = null;
    } catch (e4) {
      warnings.push("ungroup after scaling failed; the artwork stays one group. Print is unaffected, per item editing is harder");
    }

    /* The resize pivot is not the origin (it shifts in our tests). Correct with pre-scale bounds × scale.
       This keeps the page margin and puts no artwork into the artboard corner. */
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

  /* Artboard = trim rectangle, centred on the artwork. Artwork beyond bleedMm is bleed.
     Skip bounding box alignment, which would consume the page margin */
  var bPt = mm2pt(bleedMm);
  var newW = mm2pt(wmm);
  var newH = mm2pt(hmm);
  doc.artboards[0].artboardRect = [bPt, bPt + newH, bPt + newW, bPt];

  /* Illustrator 28.4.1 ExtendScript has no writable documentBleed property
     (a read throws in our tests). Enter the 3 mm bleed by hand in Document Setup before export.
     The export checklist lists it as required. */

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
      warnings.push("bleed too small on one side: minimum " + r2(ovMin) + "mm, required " + bleedMm + "mm");
    }
    if (ovMax > bleedMm + 0.5) {
      warnings.push("artwork passes the bleed edge by " + r2(ovMax) + "mm (limit ≤ " + bleedMm + "mm)");
    }
  } else if (ovMax > 0.5) {
    warnings.push("artwork passes the artboard by " + r2(ovMax) + "mm (bleed set to 0)");
  }

  var wMm = r2(pt2mm(bb.right - bb.left));
  var hMm = r2(pt2mm(bb.top - bb.bottom));
  if (!closeEnough(wMm, frameW, 0.5) || !closeEnough(hMm, frameH, 0.5)) {
    warnings.push("visible artwork " + wMm + "×" + hMm + "mm does not match the page with bleed " + frameW + "×" + frameH + "mm");
  }

  /* Structure counts */
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

  /* Save to the final path. Copy the old file as a backup first and restore it on failure.
     A rename backup rewrites the File object itself in ExtendScript,
     so the later saveAs computes the wrong extension (measured output: .ai.ai). */
  var saveOpts = new IllustratorSaveOptions();
  saveOpts.pdfCompatible = true;
  /* The default is false. Without an embedded profile, another machine reads this .ai
     with its own CMYK working space, and the FOGRA39 values lose their basis. */
  try { saveOpts.embedICCProfile = true; } catch (eICC) { warnings.push("IllustratorSaveOptions rejects embedICCProfile; ICC not embedded"); }

  var backupPath = outPath + ".prev";
  var backup = new File(backupPath);
  var finalFile = new File(outPath);

  /* A run that dies between backup done and save failed leaves a .prev file here.
     That file can be older than the current deliverable: the last save may have succeeded and only the backup removal failed.
     Roll back only when the backup is newer. Otherwise delete the stale backup, so a newer deliverable stays. */
  if (backup.exists) {
    var backupTime = 0, currentTime = 0;
    try { backupTime = backup.modified.getTime(); } catch (eB) {}
    try { if (finalFile.exists) currentTime = finalFile.modified.getTime(); } catch (eC) {}
    var shouldRestore = !finalFile.exists || backupTime > currentTime;
    if (shouldRestore) {
      if (finalFile.exists && !finalFile.remove()) {
        throw new Error("backup " + backupPath + " is newer than the deliverable, and the damaged deliverable will not delete; stopped");
      }
      if (!backup.copy(outPath)) {
        throw new Error("backup " + backupPath + " is newer than the deliverable, but restore failed; stopped");
      }
      warnings.push("restored " + outPath + " from a leftover backup (backup newer than the deliverable)");
    }
    backup.remove();
    finalFile = new File(outPath);
  }

  var hadPrevious = finalFile.exists;
  if (hadPrevious && !finalFile.copy(backupPath)) {
    throw new Error("cannot back up the existing " + outPath + "; stopped to keep it");
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
