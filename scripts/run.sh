#!/bin/bash
# 单次 Illustrator 任务的执行器。
#   run.sh <job-id> <script.jsx>
#
# 只做五件事：校验参数与环境、保证单实例、发一个运行令牌、把 JSX 交给 Illustrator、校验结果归属。
# 业务逻辑全在 JSX 里，这里不解释结果。

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SPEC="$ROOT/config/print-spec.json"

usage() {
  echo "usage: run.sh <job-id> <script.jsx>" >&2
  echo "example: run.sh 2026-11-03-flyer-a3 02-import-convert.jsx" >&2
  exit 64
}

[ $# -ge 2 ] || usage
JOB_ID="$1"
SCRIPT="$2"

if [[ ! "$JOB_ID" =~ ^[a-z0-9][a-z0-9-]{2,60}$ ]]; then
  echo "invalid job id: '$JOB_ID' (allow: lowercase letters, digits, hyphen)" >&2
  exit 64
fi

case "$SCRIPT" in
  *..*) echo "invalid script path: $SCRIPT" >&2; exit 64 ;;
esac

JOB="$ROOT/jobs/$JOB_ID"
[ -d "$JOB" ] || { echo "job dir not found: $JOB" >&2; exit 66; }

JSX="$ROOT/scripts/$SCRIPT"
[ -f "$JSX" ] || { echo "script not found: $JSX" >&2; exit 66; }

spec_get() {
  python3 -c 'import json,sys
d=json.load(open(sys.argv[1]))
for k in sys.argv[2].split("."):
    d=d[k] if isinstance(d,dict) else d[int(k)]
print(d if not isinstance(d,bool) else str(d).lower())' "$SPEC" "$1"
}

TIMEOUT="$(spec_get illustrator.timeoutSeconds)"
MIN_VERSION="$(spec_get illustrator.minimumVersion)"

# shellcheck source=scripts/lib/illustrator.sh
. "$ROOT/scripts/lib/illustrator.sh"

APP_PATH="$(ai_app_path "$ROOT")"

# 配置本身坏掉时早失败，别让空值变成奇怪的运行行为
if [ -z "$APP_PATH" ] || [ ! -d "$APP_PATH" ]; then
  echo "config error: no Illustrator found; set illustrator.appPath or install Illustrator 2024+" >&2
  exit 78
fi
if ! [[ "$TIMEOUT" =~ ^[0-9]+$ ]] || [ "$TIMEOUT" -lt 30 ]; then
  echo "config error: illustrator.timeoutSeconds must be an integer >= 30 (got '$TIMEOUT')" >&2
  exit 78
fi
if [ -z "$MIN_VERSION" ]; then
  echo "config error: illustrator.minimumVersion is empty" >&2
  exit 78
fi

# 单实例锁：Illustrator 只有一个前台文档，并行跑必然互相破坏
LOCK="${TMPDIR:-/tmp}/rgb-cmyk-preprint.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "another print run is in progress ($LOCK)" >&2
  echo "if it is a leftover from a crash, verify Illustrator is idle, then: rm -rf '$LOCK'" >&2
  exit 75
fi

AS_FILE=""
cleanup() {
  # 超时的情况下不释放锁：Illustrator 里的 JSX 可能还在跑，重叠执行比卡住更危险
  if [ -f "$LOCK/timeout" ]; then
    echo "锁保留在 $LOCK —— 上次运行超时，Illustrator 里可能仍有脚本在跑。" >&2
    echo "确认 Illustrator 空闲后执行: rm -rf '$LOCK'" >&2
  else
    rmdir "$LOCK" 2>/dev/null
  fi
  [ -n "$AS_FILE" ] && rm -f "$AS_FILE"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

VER=$(ai_version "$APP_PATH")
if [ -z "$VER" ]; then
  echo "cannot read Illustrator version from: $APP_PATH" >&2
  exit 78
fi
if ! ai_version_ge "$VER" "$MIN_VERSION"; then
  echo "Illustrator too old: got $VER, need $MIN_VERSION or newer" >&2
  exit 78
fi

if ! pgrep -f "$APP_PATH/Contents/MacOS" >/dev/null 2>&1; then
  echo "launching Illustrator $VER ..."
  open -a "$APP_PATH"
  for _ in $(seq 1 90); do
    pgrep -f "$APP_PATH/Contents/MacOS" >/dev/null 2>&1 && break
    sleep 1
  done
  sleep 3
fi

# 运行令牌：防止读到上一次或别的任务留下的 result.json
TOKEN="$(python3 -c 'import secrets;print(secrets.token_hex(8))')"
printf '%s' "$TOKEN" > "$JOB/.run-token"

AS_FILE="${TMPDIR:-/tmp}/rgb-cmyk-preprint-$$.applescript"
if ! python3 - "$JOB" "$SPEC" "$JSX" "$AS_FILE" <<'PY'
import json, sys

job, spec, jsx, out = sys.argv[1:5]
code = (
    "var JOB_DIR = %s; var SPEC_PATH = %s; $.evalFile(new File(%s));"
    % (json.dumps(job), json.dumps(spec), json.dumps(jsx))
)
as_src = (
    'tell application id "com.adobe.illustrator"\n'
    '  set r to do javascript %s\n'
    '  return r\n'
    'end tell\n'
) % json.dumps(code)
open(out, "w").write(as_src)
PY
then
  echo "failed to generate AppleScript wrapper" >&2
  exit 70
fi

OUT_LOG="$JOB/run.log"
{
  echo "== $(date '+%F %T') run $SCRIPT on $JOB_ID (token $TOKEN)"
  echo "== Illustrator $VER"
} >> "$OUT_LOG"

rm -f "$JOB/result.json"

osascript "$AS_FILE" >> "$OUT_LOG" 2>&1 &
PID=$!

(
  sleep "$TIMEOUT"
  if kill -0 "$PID" 2>/dev/null; then
    echo "TIMEOUT after ${TIMEOUT}s — Illustrator is likely blocked by a modal dialog" >> "$OUT_LOG"
    touch "$LOCK/timeout"
    kill -9 "$PID" 2>/dev/null
  fi
) >/dev/null 2>&1 &
WATCH=$!

wait "$PID"
RC=$?

kill "$WATCH" 2>/dev/null
wait "$WATCH" 2>/dev/null

if [ -f "$LOCK/timeout" ]; then
  echo "run timed out after ${TIMEOUT}s, see $OUT_LOG" >&2
  exit 71
fi

if [ $RC -ne 0 ]; then
  echo "osascript failed (rc=$RC). tail of $OUT_LOG:" >&2
  tail -n 12 "$OUT_LOG" >&2
  exit 70
fi

RESULT="$JOB/result.json"
if [ ! -f "$RESULT" ]; then
  echo "script finished but wrote no result.json: $RESULT" >&2
  exit 70
fi

EXPECTED_SCRIPT="${SCRIPT%.jsx}"

# QA 时给交付物绑定内容哈希；同大小替换会在 04 报告里失效。
if [ "$EXPECTED_SCRIPT" = "03-qa" ]; then
  python3 - "$RESULT" "$JOB" <<'PY'
import hashlib, json, os, sys
result_path, job_dir = sys.argv[1:3]
try:
    job = json.load(open(os.path.join(job_dir, "job.json"), encoding="utf-8"))
    rel = (job.get("output") or {}).get("ai")
    path = os.path.join(job_dir, rel) if rel else None
    if path and os.path.exists(path):
        d = json.load(open(result_path, encoding="utf-8"))
        d["deliverableSha256"] = hashlib.sha256(open(path, "rb").read()).hexdigest()
        json.dump(d, open(result_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        qa_path = os.path.join(job_dir, "work", "qa-result.json")
        if os.path.exists(qa_path):
            q = json.load(open(qa_path, encoding="utf-8"))
            q["deliverableSha256"] = d["deliverableSha256"]
            json.dump(q, open(qa_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
except Exception as exc:
    print("could not bind deliverable hash: %s" % exc, file=sys.stderr)
PY
fi
if ! python3 - "$RESULT" "$EXPECTED_SCRIPT" "$JOB_ID" "$TOKEN" <<'PY'
import json, sys

path, script, job_id, token = sys.argv[1:5]
d = json.load(open(path, encoding="utf-8"))
problems = []
if d.get("script") != script:
    problems.append(f"script mismatch: got {d.get('script')!r}, expected {script!r}")
if d.get("jobId") != job_id:
    problems.append(f"jobId mismatch: got {d.get('jobId')!r}, expected {job_id!r}")
if d.get("runToken") != token:
    problems.append("runToken mismatch: result is stale or from another run")
for p in problems:
    print("RESULT-REJECT " + p, file=sys.stderr)
print(json.dumps(d, ensure_ascii=False, indent=2))
sys.exit(1 if problems else (0 if d.get("ok") else 2))
PY
then
  RC=$?
  if [ $RC -eq 2 ]; then
    echo "script reported failure, see $RESULT" >&2
    exit 65
  fi
  exit 70
fi
