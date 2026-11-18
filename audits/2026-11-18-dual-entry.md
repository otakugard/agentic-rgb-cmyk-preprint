# Dual-entry and RCA regression audit

Date: 2026-11-18
Baseline: `768d7a0` (`origin/main`)
Branch: `feat/srgb-pdf-entry`

Scope: source entry, geometry, ICC binding, Gate 3 structure checks, generated
fixtures, and public documentation. No client artwork or private job was used.

## What changed

- Added `source.kind: "srgb-pdf"` for an existing sRGB PDF at final size. The
  source page is trim plus bleed at `1:1`; a differently sized PDF stops at
  `01d`. Figma exports keep `figma.pxToPt` (`0.75`).
- Removed the library-level FOGRA39 fallback. Audit and render use the ICC
  recorded in `work/conversion.json`; the report still binds the configured
  delivery ICC.
- Added Gate 3 checks: `/DefaultCMYK`, ICC chain, inherited colour spaces,
  stored ink, managed separations, independent renders, and an optional fold
  declaration.
- Added generated negative fixtures in `scripts/13-rca-selftest.py`. The test
  creates its own PDFs and does not read signed-off files.
- Added a direct-PDF example, dual-entry README instructions, and pipeline,
  gate, and limitation notes.

## Verification

```text
$ ./install/verify.sh --full
FAIL=0 WARN=1
Environment is ready for a job. Run from the repository root.

$ python3 scripts/13-rca-selftest.py
{"ok": true, "counts": {"checks": 27, "pass": 27, "fail": 0}}

$ python3 scripts/08-gate3.py 2026-01-02-a3-fold-srgb --selftest
{"overall": "fail", "counts": {"pass": 25, "fail": 9, "pending": 0}, "selftest": true}
```

Synthetic direct-entry job `2026-01-02-a3-fold-srgb`:

```text
source: existing sRGB PDF, 426 × 303 mm, 3 mm bleed
01-prepare: cmyk.pdf 426 × 303 mm, scale 1.0
02-import: 100%, artboard 420 × 297 mm, artwork 426 × 303 mm
Gate 1: pass
Gate 2: pass
Gate 3: 34 pass / 0 fail / 0 pending
QA: 46 pass / 0 fail / 4 pending (AI profile, thin strokes, tolerance, Proof Setup)
```

The synthetic job exercised `pdf.defaultcmyk`, `pdf.profilechain`,
`pdf.inheritedcs`, `pdf.inkStored`, `pdf.separationsManaged`,
`pdf.independentRender`, and `pdf.fold`. The removal probe reported
`defaultcmyk_reseparation_confirmed_by_removal_probe`.

A second synthetic Figma-size job `2026-01-02-a3-fold-figma` (source page
`568 × 404 mm`, scale `0.75`) reached Gate 2 pass and Gate 3
`34 / 0 / 0`. Gate 1 stayed PENDING because no Figma snapshot covered the
synthetic node, which is the intended state. It produced
`426 × 303 mm` at `02`.

## Limits

- The synthetic job covers the direct entry. A real Figma MCP export and its
  snapshot window were not exercised in this audit.
- The fold check validates geometry only; text across the fold, imposition and
  creep remain a human review.
- PDF/X is an internal structure check. Acrobat Preflight and the print shop's
  written acceptance remain external.
- The final publish scan for credentials and client markers is separate.

## Result

PASS for the code and documentation scope. The direct sRGB PDF entry reaches
the same candidate and gate chain as a Figma export.
