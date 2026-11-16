# Gates

Three gates, three questions. A missing check stays PENDING.

## Gate 1: source integrity

| Check | Evidence |
|---|---|
| Source PDF unchanged | SHA-256 before and after |
| RGB working copy is sRGB | embedded ICC description and hash |
| Palette matches | HEX set from source and copy |
| Size matches | trim size within 0.2 mm |
| Figma node unchanged | before/after snapshot of the node |

If no Figma snapshot covers the node, the check is PENDING. A snapshot must
cover this node on this page.

## Gate 2: conversion integrity

| Check | Evidence |
|---|---|
| Actual CMYK tuples match the AI content stream | object-level extraction from the content stream |
| Invisible Type3 text and masks do not create false failures | context + 300 dpi visible pixels = 0 |
| CMYK-vs-AI visual match | flat-colour median, p95, over-threshold area |
| Source-vs-simulation match | ICC prediction; paper measurement is the press check |
| Structure | no RGB, no spot, no `/Separation /All` outside marks |
| Bleed | full 3 mm band, zero white pixels |
| TAC | visible maximum against the confirmed limit |
| Control probe | actual conversion vs ICC prediction |

An authorised colour override (single-K) produces a predictable deviation. The
report records it as PENDING.

## Gate 3: output structure

| Check | Evidence |
|---|---|
| TrimBox / BleedBox / MediaBox | exact values from `job.json` and `marks.json` |
| OutputIntent | GTS_PDFX + delivery ICC hash |
| PDF/X-4 structure | version in Info and XMP, `/Trapped`, no `/PS`, no Action/JS, no encryption |
| No RGB operators | content stream scan, object streams included |
| Fonts | no unembedded font; Type3 shells must not paint |
| Marks | count and geometry, outside TrimBox |
| Bleed content | rasterised band of actual ink across the full bleed |
| TAC | raster plus visible vector maximum; registration marks are excluded from the raster crop |
| Overprint | ExtGState and content-stream usage |

When the PDF uses overprint, `pdf.overprint` stays PENDING until the print shop
confirms the overprint and trapping policy in writing. When no overprint state
is present, the check passes as a verified knockout. An AI-side claim
("all fills are knockout") needs `work/ai-color-audit.json`; without it the
report states an evidence gap instead.

## What Gate 3 covers

Gate 3 confirms that the candidate PDF has the structure this pipeline checks.

## What stays outside Gate 3

| Item | Owner |
|---|---|
| Acrobat Preflight agreement | a separate engine |
| Acceptance of ICC, TAC, overprint, or trapping | the print shop, in writing |
| Match between the contract proof and the press | the proof |
| Correctness of the artwork | Figma, the design source |

`qa.json` always sets `approval.required: true`.

## Status semantics

| Status | Meaning |
|---|---|
| PASS | a real check ran and passed |
| FAIL | confirmed mismatch or prepress defect; do not deliver |
| PENDING | not generated, not verified, or needs print shop / manual GUI / physical proof |
| WARN | evidenced, non-blocking, needs a human decision |

`ok: true` reports that the script completed. Acceptance lives in `qa.json`
(`approval.required`).
