# Start audit — source subtree (redacted)

- Auditor: `codex exec`, model `gpt-6.1-sol`, reasoning `high`, sandbox `read-only`
- Scope: the private `print-agent/` subtree, before productization
- Method: read-only; no files modified; no web access
- Redaction: every client identifier, key, node id, and colour value is removed
  from this copy. The raw report stays local and is not published.

## Verdict

The source subtree is **not publishable as-is**. It carries client identity in
the configuration, the colour vocabulary, the docs, and one lock filename.
The pipeline code itself is generic.

## Finding classes

| Class | Files | Count | Example location |
|---|---|---|---|
| Figma file key + design URL | `config/print-spec.json` | 1 | `print-spec.json` figma block |
| Client and team names | `config/print-spec.json`, `color/brand-map.json`, `scripts/run.sh` | 3 | config figma block, brand map notes, temp lock name |
| Team id and folder id | `config/print-spec.json` | 2 | figma block |
| Figma node ids | `docs/01-figma.md`, `docs/04-incidents.md` | 12 | page and frame references |
| Brand palette (14 colours) | `color/brand-map.json` | 14 | named semantic colours |
| Job records and delivery batches | `README.md`, `docs/04-incidents.md`, `docs/05-constraints.md` | 5 | results sections |
| Absolute user paths | runtime caches only | 0 in tracked source | probe cache |

## Files safe to publish without content changes

- `config/marks.json`
- `scripts/01-prepare.sh`
- `scripts/04-report.py`
- `tests/preflight-selftest.json`
- `docs/02-illustrator.md` (generic system paths and Adobe names only)

## Licensing risks noted

| Area | Risk |
|---|---|
| ICC profiles | Redistribution terms not stated; a mirror was mentioned in the docs |
| Fonts and icons | Font names appear in the docs; no redistribution or outlining permission was recorded |
| Embedded font subset | An embedded Adobe subset is described in an incident record |
| Adobe product names | Product names only; no binaries redistributed |

## Assumptions that break when `print-agent/` becomes the repo root

1. `print-spec.json` → `print.iccProfile` began with a `print-agent/` prefix.
2. `04-report.py` emitted a command with the same prefix.
3. Docs referenced `.opencode/` and `Now/`, which belong to the parent project.
4. No install guide, no dependency manifest, no ICC acquisition procedure.

## Install gaps found

- Python version and `pypdf` / `numpy` were undeclared.
- LittleCMS binaries (`tificc`, `transicc`) were assumed to be on `PATH`.
- macOS Automation permission for Illustrator was undocumented.
- qpdf and MuPDF were named in incident notes, not in requirements.
- ICC acquisition was undocumented.

## Actions taken in the public repository

| Finding | Action |
|---|---|
| Figma key, team id, folder id | config fields emptied; job values belong in `job.json` |
| Brand palette | replaced with an empty, documented schema |
| Node ids and job records | not copied; docs rewritten as generic limits |
| `print-agent/` path prefixes | removed from config and report output |
| Lock filename | renamed |
| Hardcoded `/opt/homebrew/bin` tools | resolved through `PATH`, with env overrides |
| No dependency manifest | `requirements.txt` added |
| No install path | `install/install.sh`, `install/verify.sh`, manual and agent install docs added |
