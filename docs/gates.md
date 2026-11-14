# Gates

Three gates, three questions. A gate never upgrades a missing check to PASS.

## Gate 1 — source integrity

| Check | Evidence |
|---|---|
| Source PDF unchanged | SHA-256 before and after |
| RGB working copy is sRGB | embedded ICC description and hash |
| Palette matches | HEX set from source and copy |
| Size matches | trim size within 0.2 mm |
| Figma node unchanged | before/after snapshot of the node |

If no Figma snapshot covers the node, the check is PENDING. A snapshot of a
different page does not count.

## Gate 2 — conversion integrity

| Check | Evidence |
|---|---|
| Actual CMYK tuples match the AI content stream | object-level extraction, not `fills` |
| Invisible Type3 text and masks do not create false failures | context + 300 dpi visible pixels = 0 |
| CMYK-vs-AI visual match | flat-colour median, p95, over-threshold area |
| Source-vs-simulation match | ICC prediction, not paper measurement |
| Structure | no RGB, no spot, no `/Separation /All` outside marks |
| Bleed | full 3 mm band, zero white pixels |
| TAC | visible maximum against the confirmed limit |
| Control probe | actual conversion vs ICC prediction |

An authorised colour override (single-K) produces a predictable deviation. It
is reported as PENDING, not PASS and not FAIL.

## Gate 3 — output structure

| Check | Evidence |
|---|---|
| TrimBox / BleedBox / MediaBox | exact values from `job.json` and `marks.json` |
| OutputIntent | GTS_PDFX + delivery ICC hash |
| PDF/X-4 structure | version in Info and XMP, `/Trapped`, no `/PS`, no Action/JS, no encryption |
| No RGB operators | content stream scan, object streams included |
| Fonts | no unembedded font; Type3 shells must not paint |
| Marks | count and geometry, outside TrimBox |
| Bleed content | rasterised band, not only the boxes |
| TAC | raster plus visible vector maximum; registration marks are excluded from the raster crop |
| Overprint | ExtGState and content-stream usage |

When the PDF uses overprint, `pdf.overprint` stays PENDING until the print shop
confirms the overprint and trapping policy in writing. When no overprint state
is present, the check passes as a verified knockout. An AI-side claim
("all fills are knockout") needs `work/ai-color-audit.json`; without it the
report states an evidence gap instead.

## What a PASS means

Gate 3 PASS means: the candidate PDF has the structure this pipeline checks. It
does not mean:

- Acrobat Preflight agrees (that is a separate engine).
- The print shop accepts the ICC, TAC, overprint, or trapping.
- The contract proof matches the press.
- The artwork is correct.

`qa.json` always sets `approval.required: true`.

## Status semantics

| Status | Meaning |
|---|---|
| PASS | a real check ran and passed |
| FAIL | confirmed mismatch or prepress defect; do not deliver |
| PENDING | not generated, not verified, or needs print shop / manual GUI / physical proof |
| WARN | evidenced, non-blocking, needs a human decision |

`ok: true` only means the script completed. It is not acceptance.
