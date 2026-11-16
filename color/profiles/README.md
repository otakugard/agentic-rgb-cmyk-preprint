# ICC profiles

You supply the ICC profiles. Install them locally and point
`config/print-spec.json` → `print.iccProfile` at the file you use.

## Required

### Delivery CMYK profile

Default: **ISO Coated v2 300% (ECI)** for FOGRA39L, TAC 300%.

Where to get it:

1. ECI publishes the ISO Coated v2 family at <https://www.eci.org/>. Take the
   300% variant, not the 330% one.
2. Adobe apps often ship FOGRA39 profiles under
   `/Library/Application Support/Adobe/Color/Profiles/Recommended/`.
   `CoatedFOGRA39.icc` allows TAC 330%. The pipeline target is 300%.

Verify before use:

```bash
python3 - <<'PY'
p = "color/profiles/iso-coated-v2-300.icc"
d = open(p, "rb").read()
print(len(d), "bytes", "space=" + d[16:20].decode("ascii", "replace"))
PY
```

Bytes 16–19 are the data colour space. A CMYK profile prints `space=CMYK`.

### Source RGB profile

Figma exports embed sRGB2014. You need an sRGB profile for the audit and proof
steps. macOS has one at
`/System/Library/ColorSync/Profiles/sRGB Profile.icc`. Adobe ships
`sRGB IEC61966-2.1.icc` in the Recommended folder.

The pipeline reads the source ICC from the PDF. `print.iccProfile` is the
destination only.

## Naming

Any filename works. Keep the name descriptive and record the SHA-256 in your
job notes:

```bash
shasum -a 256 color/profiles/*.icc
```

## Profile rules

- Keep `CoatedFOGRA39.icc` under its own name. The TAC differs.
- Use a profile whose name and source you can state.
- Commit ICC files to a public fork only with redistribution rights. Keep them
  local; `.gitignore` covers this directory.
