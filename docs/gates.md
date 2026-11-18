# Gates

Three gates, three questions. A missing check stays PENDING.

## Gate 1: source integrity

| Check | Evidence |
|---|---|
| Source PDF unchanged | SHA-256 before and after |
| RGB working copy is sRGB | embedded ICC description and hash |
| Palette matches | HEX set from source and copy |
| Size matches | trim size within 0.2 mm |
| Figma node unchanged | before/after snapshot of the node, matched by `nodeId` value |
| Direct sRGB PDF source | source hash, embedded sRGB profile, final-size page geometry, RGB copy and visual comparison |

A Figma job needs a snapshot that covers this node on this page. A direct
`srgb-pdf` job states `figmaSnapshot: not applicable`; it does not invent a
node or a snapshot. The source PDF hash, embedded profile and page geometry
carry Gate 1 for that entry.

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
| `/DefaultCMYK` | one ICCBased array on the page, N=4, delivery ICC; no form override |
| ICC binding | config, conversion record, DestOutputProfile and `/DefaultCMYK` share one SHA-256 |
| Inherited colour spaces | page and Form `cs`/`CS` names resolve; CMYK ICC matches; no RGB space |
| Stored ink | content-stream `k`/`K` and four-component `scn`/`SCN` operands |
| Managed separations | stored tuples versus rendered plates; removal probe restores a stored tuple |
| Independent render | Ghostscript and MuPDF agree on the same file inside TrimBox |
| Fold | optional half-fold line at trim centre and equal panel widths |

When the PDF uses overprint, `pdf.overprint` stays PENDING until the print shop
confirms the overprint and trapping policy in writing. When no overprint state
is present, the check passes as a verified knockout. An AI-side claim
("all fills are knockout") needs `work/ai-color-audit.json`; without it the
report states an evidence gap instead.

## What Gate 3 covers

Gate 3 confirms that the candidate PDF has the structure this pipeline checks.
Stored ink values, device-space plate readings and rendered appearance are
separate evidence. A plate reading does not replace the content-stream value.

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
