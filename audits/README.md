# Audits

Two read-only audits, run with `codex exec` (model `gpt-6.1-sol`, reasoning
`high`, sandbox `read-only`). No files were modified by the auditor and no web
search was used.

| Round | Scope | Verdict | Report |
|---|---|---|---|
| Start | the private source subtree, before productization | not publishable as-is | [`start-audit.md`](start-audit.md) (redacted) |
| End | this public repository, four review passes | **PASS** | [`end-audit.md`](end-audit.md) |

The start report is redacted: client names, keys, node ids, and colour values
were removed from the published copy. The end audit ran four passes because each
pass found real defects; the findings below were fixed before release.

## Defects found and fixed

### Release blockers

- Registration marks selected a **nonstroking** colour space and then stroked,
  so marks printed in the default colour. They now use `/CSAll CS` + `1 SCN`.
- The job examples omitted `pdfExport.layout` and `contract`, so a generated
  candidate failed Gate 3 origin and separation checks.

### Gate correctness

- Type3 painting detection missed `f*`.
- The PDF/X action scan only saw raw bytes; compressed object streams and
  annotation actions (`/S /Launch`, `/URI`, ...) could pass.
- Font checking computed the Type3 paint count but ignored it.
- Gate 2 did not include TAC.
- A stale or missing `gate3.json` could still be classified as external-only.
- Acceptance hashes omitted `config/marks.json`.

### Data integrity

- `01-prepare.sh` re-recorded the source hash on every run; a replaced source
  could reset the comparison baseline. The first hash is now pinned in
  `job.json` and a change stops the step.
- `10-gray-to-k.py --apply` replaced `cmyk.pdf` even when validation failed, and
  its freshness guard rejected a legitimate second application.
- `scaleInPdf: false` left no `cmyk-final.pdf`, so `07c` could not run. `01d`
  now always produces it.

### Evidence honesty

- `11-overprint-audit.py` claimed "all fills are knockout" when the AI evidence
  file was missing. It now reports an evidence gap.
- Gate 3 overprint status contradicted the documented approval rule; the docs
  now describe the actual behaviour.
- `06-color-audit.py` crashed on unmatched colours (`max()` over `None`).
- An empty brand map made every colour unnamed and Gate 2 permanently PENDING;
  rows now fall back to `descriptive`.

### Operations

- `verify.sh` hardcoded Illustrator and Ghostscript paths; it now reads
  `config/print-spec.json`.
- LittleCMS is a separate `little-cms2` package, not a Ghostscript dependency.
  It is installed, checked, and required.
- Runtime steps now use the configured Ghostscript binary.
- `07b-export-pdf.jsx` referenced an undeclared variable.
- The public docs and source no longer contain a historical job label.

## Limits of this audit

Both audits are static reviews. They did not run Illustrator, did not write a
candidate PDF during review, and did not verify print output. ICC redistribution
terms remain the user's responsibility; this repository ships no ICC files.
