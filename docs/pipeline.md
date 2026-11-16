# Pipeline

All commands run from the repository root. Scripts resolve `config/`, `color/`,
and `jobs/` from the repo root, not from the current directory.

## Job directory

```
jobs/<job-id>/
  job.json                 parameters for this job
  source/                  RGB original from Figma (never modified)
  work/                    intermediates and evidence (git-ignored)
  <job-id>.ai              produced by step 02
  <job-id>-*.pdf           candidate PDF from 07c
  prepare.json             conversion parameters and hashes
  preflight.json           05 result
  color-audit.json         colour mapping
  color-mapping.md         human-readable mapping
  proof.json               three-way comparison
  gate3.json               08 result
  qa.json / qa.md / checklist.md   final report
```

`job-id` is `YYYY-MM-DD-<slug>`. `output.ai` must contain the job id.
Illustrator caches absolute paths; a generic name such as `print.ai` can become
unopenable.

## job.json

```json
{
  "jobId": "2026-01-01-example",
  "figma": { "fileKey": "...", "nodeId": "12:34" },
  "trim": { "preset": "A4" },
  "bleedMm": 3,
  "scaleInPdf": true,
  "source": { "pdf": "source/12-34.pdf", "sha256": "..." },
  "output": { "ai": "2026-01-01-example.ai", "pdf": "2026-01-01-example.pdf" },
  "pdfxStandard": "PDF/X-4:2010",
  "tacLimitPercent": 300,
  "pdfExport": { "fileName": "2026-01-01-example.pdf", "contract": "pdfx-4-2010",
                 "layout": "marks", "marginMm": 14 }
}
```

- `trim` is `{ "preset": "A4" }` or `{ "w": 210, "h": 297 }` in mm.
- `scaleInPdf: true` makes `02` import `cmyk-final.pdf`. `01d` always runs and
  always writes `cmyk-final.pdf`; `07c` reads only that file.
- `pdfExport.fileName` is required by `07c`. `marginMm` defaults to 14.
- Leave `tacLimitPercent` and `pdfxStandard` null until the print shop confirms,
  or set them explicitly for this job.
- `source.sha256` is optional at creation; the first `01` run records it.

## Steps

### 01-prepare.sh

Reads the source PDF, extracts the embedded ICC, runs `01a2` when Alpha cut
masks exist, then converts once with Ghostscript.

```
Relative Colorimetric, BPC on, OverrideICC=false,
DefaultRGBProfile=<source ICC>, OutputICCProfile=<config ICC>
```

Produces `work/source-audit.json`, `work/source-icc.icc`,
`work/conversion.json`, `work/cmyk.pdf`, `prepare.json`, and always
`work/cmyk-final.pdf` plus `work/scale-final.json` from `01d`.

`01a-source-audit.py` pins the first source SHA-256 into `job.json`. A later
`01` run with a different source hash stops: `source/` is immutable.

Rejects multi-page sources, non-ICC RGB, CMYK input, mixed RGB/CMYK, and a
non-RGB source ICC.

### 01b-rgb-audit.jsx

Opens `source/` read-only, records what Illustrator sees, exports a 150 dpi
reference TIFF. Never saves. Never overwrites the source.

### 02-import-convert.jsx

Opens `cmyk-final.pdf` when scaled, else `cmyk.pdf`. Groups the layer, sets the
artboard to trim, keeps the artwork 3 mm past the artboard, saves `<job-id>.ai`.
The script handles geometry and colour mode; the CMYK values stay as converted.
Stops if the document is not CMYK.

### 03-qa.jsx

Document-level QA: colour mode, artboard, bleed, CMYK histogram, spot colours,
bitmaps, links, text frames, thin strokes, four-colour grey. Binds the AI
SHA-256.

### 05-preflight.py

Reads the original `cmyk.pdf` (object streams included) and checks:

- 3 mm bleed band on all four sides, converted from trim coordinates
- TAC at 150/300 dpi plus exact vector values
- pure K, four-colour grey, neutral dark
- spot colours, `/Separation /All`, overprint, Type3, SMask

### 06-color-audit.py

Extracts the real solid colours from the source, converts control patches with
the same Ghostscript parameters, matches them against the actual `cmyk.pdf`
tuples, and reports ICC-predicted Lab / DeltaE00. All DeltaE00 values are
predictions, not measurements.

Reads `work/konly-targets.json` when present and forces those tuples, so an
authorised override is an explicit input. Without that file, the match step picks
the nearest CMYK tuple present in `cmyk.pdf`.

### 07-proof.py

Produces the RGB original, FOGRA39 relative simulation, absolute paper-white
simulation, and CMYK-vs-AI comparison. Pixel differences locate problems; they
are not DeltaE00 data.

### 07c-finalize-pdf.py

Builds the candidate PDF from `work/cmyk-final.pdf`: trim/bleed boxes, Japanese
double-line marks and cross marks from `config/marks.json` (stroked in
`/Separation /All` 100%), `/DefaultCMYK` and
the GTS_PDFX OutputIntent with the delivery ICC, XMP `GTS_PDFXVersion`, and
Info `/GTS_PDFXVersion` + `/Trapped`.

`07c` builds the PDF itself. Illustrator 28.4.1 hangs on the PDF/X dialog when
driven by AppleScript.

### 08-gate3.py

Checks the candidate PDF. Writes `gate3.json` bound to the PDF hash, the AI
hash, and an acceptance hash over the parameters that produced the result.

`--selftest` runs the same checks against `work/cmyk.pdf` (not a delivery PDF)
and writes `gate3-selftest.json`. The fixture must FAIL; that proves the checker
can fail.

### 04-report.py

Merges all evidence into `qa.json`, `qa.md`, and a print checklist. Binds source,
ICC, config, intermediate, AI, and PDF hashes.

## Rerun propagation

| Changed | Rerun |
|---|---|
| `source/` | 01, 01b, 01d, 02, 03, 05, 06, 07, 07c, 08, 04 |
| `print-spec.json` conversion fields (ICC, intent, BPC, image encoding) | 01, 01d, 02, 03, 05, 06, 07, 07c, 08, 04 |
| other `print-spec.json` fields (trim, bleed, PDF/X) | 01d, 02, 03, 05, 06, 07, 07c, 08, 04 |
| `cmyk.pdf` (including `10`) | 01d, 02, 03, 05, 06, 07, 07c, 08, 04 |
| `cmyk-final.pdf` (overprint, flattening) | 02, 03, 07, 07c, 08, 04 |
| `.ai` | 03, 07, 08, 04 |
| `brand-map.json` | 06, 08, 04 |
| `marks.json` | 07c, 08, 04 |

Skipping a rerun lets a stale gate vouch for a new file. Hash binding reports the
stale gate when the upstream hash changed.
