#!/bin/bash
# Illustrator discovery and version checks.
#
# The pipeline requires Illustrator 2024 (28.0) or newer. It is not pinned to
# one release: a newer install is used as-is. Set config/print-spec.json
# illustrator.appPath to force a specific install; leave it empty to use the
# newest one found in /Applications.

# ai_app_path <repo-root>
# Print the Illustrator .app path, or nothing when none is found.
ai_app_path() {
  local root="$1"
  local configured=""
  configured="$(python3 -c 'import json,sys
try:
    spec = json.load(open(sys.argv[1]))
    print((spec.get("illustrator") or {}).get("appPath") or "")
except Exception:
    print("")' "$root/config/print-spec.json" 2>/dev/null)"

  if [ -n "$configured" ] && [ -d "$configured" ]; then
    printf '%s\n' "$configured"
    return 0
  fi

  # Newest install first. Version-sort the matching bundle directories.
  local candidate
  while IFS= read -r candidate; do
    [ -n "$candidate" ] && [ -d "$candidate" ] || continue
    printf '%s\n' "$candidate"
    return 0
  done < <(ls -d /Applications/Adobe\ Illustrator*/Adobe\ Illustrator.app 2>/dev/null \
             | sort -V -r)
  return 0
}

# ai_version <app-path>
ai_version() {
  defaults read "$1/Contents/Info.plist" CFBundleShortVersionString 2>/dev/null \
    || defaults read "$1/Contents/Info" CFBundleShortVersionString 2>/dev/null
}

# ai_version_ge <actual> <minimum>
ai_version_ge() {
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
