#target illustrator

/*
 * 00-smoke.jsx
 * 冒烟测试：只读，不创建文档。验证 AppleScript → ExtendScript 通路、版本、
 * 以及 run.sh 注入的 JOB_DIR / SPEC_PATH 是否可用。
 */

var RESULT_PATH = JOB_DIR + "/result.json";

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

try {
  var spec = eval("(" + readText(SPEC_PATH) + ")");
  var icc = new File(spec.print.iccProfile);

  var payload = {
    ok: true,
    script: "00-smoke",
    jobId: null,
    runToken: null,
    appName: app.name,
    appVersion: app.version,
    locale: app.locale,
    documentsOpen: app.documents.length,
    jobDir: JOB_DIR,
    specReadable: true,
    iccPresent: icc.exists,
    expectedVersionPrefix: spec.illustrator.expectedVersionPrefix,
    versionMatches: String(app.version).indexOf(spec.illustrator.expectedVersionPrefix) === 0
  };

  try {
    var jobFile = new File(JOB_DIR + "/job.json");
    if (jobFile.exists) payload.jobId = eval("(" + readText(JOB_DIR + "/job.json") + ")").jobId;
  } catch (e0) {}
  try {
    var tf = new File(JOB_DIR + "/.run-token");
    if (tf.exists) payload.runToken = readText(JOB_DIR + "/.run-token").replace(/[\r\n]/g, "");
  } catch (e1) {}

  var parts = [];
  for (var k in payload) {
    if (!payload.hasOwnProperty(k)) continue;
    var v = payload[k];
    var t = typeof v;
    parts.push('"' + k + '":' + (t === "boolean" ? (v ? "true" : "false") : t === "number" ? String(v) : '"' + String(v).replace(/"/g, '\\"') + '"'));
  }
  writeText(RESULT_PATH, "{" + parts.join(",") + "}");
} catch (e) {
  writeText(RESULT_PATH, '{"ok":false,"script":"00-smoke","error":"' + String(e && e.message ? e.message : e).replace(/"/g, '\\"') + '"}');
}
