#!/bin/bash
# 一次性环境体检。每次开工前跑一遍，或在 README 的 P0 阶段跑。
# 只读，不修改任何东西。退出码 0 = 可以开工。

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SPEC="$ROOT/config/print-spec.json"

if [ ! -f "$SPEC" ]; then
  echo "FATAL  missing spec: $SPEC"
  exit 1
fi

FAIL=0
WARN=0

say()  { printf '%-6s %-26s %s\n' "$1" "$2" "$3"; }
ok()   { say "OK" "$1" "$2"; }
warn() { say "WARN" "$1" "$2"; WARN=$((WARN+1)); }
bad()  { say "FAIL" "$1" "$2"; FAIL=$((FAIL+1)); }

spec_get() {
  python3 -c 'import json,sys
d=json.load(open(sys.argv[1]))
for k in sys.argv[2].split("."):
    d=d[k] if isinstance(d,dict) else d[int(k)]
print(d if not isinstance(d,bool) else str(d).lower())' "$SPEC" "$1" 2>/dev/null
}

echo "preprint environment check"
echo "spec: $SPEC"
echo

# 1. Illustrator
APP_PATH="$(spec_get illustrator.appPath)"
VER_PREFIX="$(spec_get illustrator.expectedVersionPrefix)"

if [ -d "$APP_PATH" ]; then
  VER=$(defaults read "$APP_PATH/Contents/Info.plist" CFBundleShortVersionString 2>/dev/null)
  case "$VER" in
    ${VER_PREFIX}*) ok "illustrator" "$VER  ($APP_PATH)" ;;
    "")             bad "illustrator" "cannot read CFBundleShortVersionString" ;;
    *)              bad "illustrator" "$VER — expected ${VER_PREFIX}x" ;;
  esac
else
  bad "illustrator" "not found: $APP_PATH"
fi

# 2. ICC
ICC="$(spec_get print.iccProfile)"
if [ -f "$ICC" ]; then
  ok "icc profile" "$(basename "$ICC")"
else
  bad "icc profile" "not found: $ICC"
fi

# 3. PDF 预设（人工导出用，缺失不阻塞生产；PDF/X 版本由印厂定义）
PRESET="$(spec_get illustrator.pdfPresetName)"
if [ -n "$PRESET" ] && [ -f "$ROOT/templates/$PRESET.joboptions" ]; then
  ok "pdf preset" "$PRESET.joboptions"
else
  warn "pdf preset" "templates/$PRESET.joboptions 不存在（只影响人工导出，不影响 print.ai）"
fi

# 4. 运行时依赖
for bin in python3 osascript defaults pgrep transicc tificc; do
  command -v "$bin" >/dev/null 2>&1 && ok "runtime" "$bin" || bad "runtime" "missing: $bin"
done

if python3 -c "import pypdf" >/dev/null 2>&1; then
  ok "pypdf" "$(python3 -c 'import pypdf;print(pypdf.__version__)')  (Alpha→Luminosity 蒙版改写)"
else
  bad "pypdf" "missing —— python3 -m pip install --user --break-system-packages pypdf"
fi

GS_BIN="$(spec_get ghostscript.binary)"
if command -v "$GS_BIN" >/dev/null 2>&1; then
  ok "ghostscript" "$("$GS_BIN" --version 2>/dev/null | head -1)  (RGB→CMYK 的唯一转换点)"
else
  bad "ghostscript" "missing: $GS_BIN —— 没有它就无法保证 FOGRA39"
fi

# 5. 写权限
if [ -w "$ROOT/jobs" ]; then
  ok "jobs writable" "$ROOT/jobs"
else
  bad "jobs writable" "no write permission: $ROOT/jobs"
fi

echo
if [ "$FAIL" -gt 0 ]; then
  echo "RESULT  $FAIL blocking problem(s), $WARN warning(s) — 不能开工"
  exit 1
fi
echo "RESULT  ready ($WARN warning(s))"
