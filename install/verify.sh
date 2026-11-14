#!/usr/bin/env bash
# Verify the environment. Read-only: creates no files.
# Usage: ./install/verify.sh [--full]
#   --full also runs the preflight negative fixture (writes tests/preflight-selftest.json)

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 2

FULL=0
[ "${1:-}" = "--full" ] && FULL=1

FAIL=0
WARN=0

pass() { printf 'PASS  %s\n' "$1"; }
warn() { printf 'WARN  %s\n' "$1"; WARN=$((WARN + 1)); }
fail() { printf 'FAIL  %s\n' "$1"; FAIL=$((FAIL + 1)); }

section() { printf '\n== %s ==\n' "$1"; }

SPEC="$ROOT/config/print-spec.json"
cfg() {
  python3 -c 'import json,sys
d=json.load(open(sys.argv[1], encoding="utf-8"))
for k in sys.argv[2].split("."):
    d=d[k]
print(d)' "$SPEC" "$1" 2>/dev/null
}

AI_APP="$(cfg illustrator.appPath)"
AI_APP="${AI_APP:-/Applications/Adobe Illustrator 2024/Adobe Illustrator.app}"
AI_PREFIX="$(cfg illustrator.expectedVersionPrefix)"
AI_PREFIX="${AI_PREFIX:-28.}"
GS_BIN="$(cfg ghostscript.binary)"
GS_BIN="${GS_BIN:-gs}"

ver_ge() {
  # ver_ge <actual> <minimum>; numeric dotted compare, portable across BSD/GNU
  awk -v A="$1" -v B="$2" 'BEGIN {
    split(A, a, "."); split(B, b, ".");
    for (i = 1; i <= 3; i++) {
      av = a[i] + 0; bv = b[i] + 0;
      if (av > bv) exit 0;
      if (av < bv) exit 1;
    }
    exit 0;
  }'
}

section "Platform"
if [ "$(uname -s)" = "Darwin" ]; then
  pass "macOS $(sw_vers -productVersion)"
else
  fail "macOS required for Illustrator steps (found $(uname -s)); Python steps still work"
fi

section "Illustrator"
APP="$AI_APP"
if [ -d "$APP" ]; then
  AI_VER="$(defaults read "$APP/Contents/Info" CFBundleShortVersionString 2>/dev/null || true)"
  case "$AI_VER" in
    "$AI_PREFIX"*) pass "Illustrator $AI_VER (config prefix $AI_PREFIX)" ;;
    "") fail "Illustrator found but version is unreadable" ;;
    *) fail "Illustrator $AI_VER; config expects $AI_PREFIX" ;;
  esac
else
  fail "Illustrator not found at $APP (config illustrator.appPath)"
fi

section "Ghostscript"
if command -v "$GS_BIN" >/dev/null 2>&1; then
  GS_VER="$("$GS_BIN" --version 2>/dev/null || echo 0)"
  if ver_ge "$GS_VER" "10.0"; then
    pass "Ghostscript $GS_VER ($GS_BIN)"
  else
    fail "Ghostscript $GS_VER; 10.x required"
  fi
else
  fail "$GS_BIN not found (brew install ghostscript)"
fi

section "LittleCMS"
for t in tificc transicc; do
  if command -v "$t" >/dev/null 2>&1; then
    pass "$t"
  else
    fail "$t not found; 06/07 and Gate 3 need it (brew install ghostscript)"
  fi
done

section "Python"
PY="${PYTHON:-python3}"
if command -v "$PY" >/dev/null 2>&1; then
  PY_VER="$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo 0.0)"
  if ver_ge "$PY_VER" "3.11"; then
    pass "Python $PY_VER"
  else
    fail "Python $PY_VER; 3.11+ required"
  fi
  if "$PY" -c 'import venv' >/dev/null 2>&1; then
    :
  fi
  if [ -n "${VIRTUAL_ENV:-}" ]; then
    pass "virtualenv active: $VIRTUAL_ENV"
  else
    warn "no virtualenv active; run: source .venv/bin/activate"
  fi
  for mod in pypdf numpy; do
    if "$PY" -c "import $mod" >/dev/null 2>&1; then
      pass "python module $mod"
    else
      fail "python module $mod missing (pip install -r requirements.txt)"
    fi
  done
else
  fail "python3 not found"
fi

section "PDF tools"
if command -v qpdf >/dev/null 2>&1; then
  pass "$(qpdf --version 2>/dev/null | head -n1)"
else
  warn "qpdf not found; structural verification will be weaker"
fi
if command -v mutool >/dev/null 2>&1; then
  pass "$(mutool -v 2>&1 | head -n1)"
else
  warn "mutool not found; visual diff falls back to Ghostscript"
fi

section "Proof profiles"
for f in \
  "/System/Library/ColorSync/Profiles/sRGB Profile.icc" \
  "/Library/Application Support/Adobe/Color/Profiles/Recommended/CoatedFOGRA39.icc"; do
  if [ -f "$f" ]; then
    pass "present $f"
  else
    fail "missing $f (needed by 07-proof and the Gate 3 visual checks)"
  fi
done

section "Configuration"
if "$PY" - "$ROOT" <<'PY'
import json, os, sys
root = sys.argv[1]
spec_path = os.path.join(root, "config", "print-spec.json")
marks_path = os.path.join(root, "config", "marks.json")
try:
    spec = json.load(open(spec_path, encoding="utf-8"))
except Exception as exc:
    print("spec: %s" % exc); sys.exit(1)
icc = (spec.get("print") or {}).get("iccProfile") or ""
if not icc:
    print("spec: print.iccProfile is empty"); sys.exit(1)
path = icc if os.path.isabs(icc) else os.path.join(root, icc)
if not os.path.exists(path):
    print("spec: missing ICC %s" % path); sys.exit(1)
head = open(path, "rb").read(128)
if head[16:20] != b"CMYK":
    print("spec: ICC is not CMYK: %s" % path); sys.exit(1)
try:
    marks = json.load(open(marks_path, encoding="utf-8"))
except Exception as exc:
    print("marks: %s" % exc); sys.exit(1)
if marks.get("style") != "jp-double":
    print("marks: style must be jp-double"); sys.exit(1)
print("spec: %s" % spec.get("print", {}).get("iccName", "unnamed"))
print("icc:  %s" % path)
print("marks: jp-double, %.1f mm offset" % float(marks.get("offsetMm", 0)))
PY
then
  pass "config/print-spec.json and config/marks.json"
else
  fail "configuration invalid (see message above)"
fi

section "Scripts"
for f in scripts/01-prepare.sh scripts/run.sh scripts/lib/env-check.sh install/verify.sh; do
  if [ -x "$f" ]; then
    pass "executable $f"
  else
    fail "not executable: $f (chmod +x)"
  fi
done
for f in scripts/07c-finalize-pdf.py scripts/08-gate3.py scripts/09-selftest.py; do
  [ -f "$f" ] && pass "present $f" || fail "missing $f"
done

if [ "$FULL" = "1" ]; then
  section "Negative fixture"
  if "$PY" scripts/09-selftest.py >/tmp/preprint-selftest.json 2>/tmp/preprint-selftest.err; then
    if "$PY" -c 'import json,sys;d=json.load(open("/tmp/preprint-selftest.json"));sys.exit(0 if d.get("ok") else 1)'; then
      pass "09-selftest detected the TAC and bleed fixtures"
    else
      fail "09-selftest returned ok=false"
    fi
  else
    fail "09-selftest failed: $(tail -n1 /tmp/preprint-selftest.err)"
  fi
fi

section "Summary"
printf 'FAIL=%d WARN=%d\n' "$FAIL" "$WARN"
if [ "$FAIL" -gt 0 ]; then
  echo "Environment is not ready. Fix FAIL items before running a job."
  exit 1
fi
echo "Environment is ready for a job. Run from the repository root."
