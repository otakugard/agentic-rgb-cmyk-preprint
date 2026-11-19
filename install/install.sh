#!/usr/bin/env bash
# Bootstrap missing tools and Python dependencies, then verify.
# Idempotent. Does not download ICC profiles and does not run a print job.

set -eu

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

say() { printf '\n== %s ==\n' "$1"; }

say "Homebrew"
if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew is required: https://brew.sh" >&2
  exit 1
fi

install_if_missing() {
  local bin="$1"; shift
  if command -v "$bin" >/dev/null 2>&1; then
    echo "ok: $bin"
  else
    echo "installing: $*"
    brew install "$@"
  fi
}

install_if_missing gs ghostscript
command -v tificc >/dev/null 2>&1 || brew install little-cms2
install_if_missing qpdf qpdf
command -v mutool >/dev/null 2>&1 || brew install mupdf-tools

say "Python"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt

say "ICC"
ICC="$(python - <<'PY'
import os, sys
sys.path.insert(0, "scripts/lib")
try:
    import contract
    print(contract.profile(contract.load(), "target")[0])
except Exception:
    spec = json.load(open("config/print-spec.json", encoding="utf-8"))
    print(spec.get("print", {}).get("iccProfile", ""))
PY
)"
if [ -f "$ICC" ]; then
  echo "ok: $ICC"
else
  echo "missing: $ICC"
  echo "Install an ISO Coated v2 300% (ECI) profile and point the colour contract at it."
  echo "See color/profiles/README.md. Then run ./install/install.sh again."
fi

say "Verify"
./install/verify.sh --full
