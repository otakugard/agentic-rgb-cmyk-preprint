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

# 1. Illustrator（2024/28.0 或更新；空 appPath = 自动选最新安装）
. "$ROOT/scripts/lib/illustrator.sh"
APP_PATH="$(ai_app_path "$ROOT")"
MIN_VERSION="$(spec_get illustrator.minimumVersion)"
MIN_VERSION="${MIN_VERSION:-28.0}"

if [ -n "$APP_PATH" ] && [ -d "$APP_PATH" ]; then
  VER=$(ai_version "$APP_PATH")
  if [ -z "$VER" ]; then
    bad "illustrator" "cannot read CFBundleShortVersionString: $APP_PATH"
  elif ai_version_ge "$VER" "$MIN_VERSION"; then
    ok "illustrator" "$VER  ($APP_PATH)"
  else
    bad "illustrator" "$VER — need $MIN_VERSION or newer ($APP_PATH)"
  fi
else
  bad "illustrator" "not found; install Illustrator 2024+ or set illustrator.appPath"
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
