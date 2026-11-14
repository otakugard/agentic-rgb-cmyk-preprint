# agentic-rgb-cmyk-preprint

An evidence-first prepress pipeline that turns a Figma RGB export into a CMYK
Illustrator file and a PDF/X-4 candidate, with machine-checkable gates.

It is built for one narrow job: **Figma → Illustrator (macOS) → PDF/X-4**, with
Ghostscript as the only colour conversion point and no silent fixes.

```
Figma PDF (sRGB, no OutputIntent)
  → colour audit + Ghostscript CMYK conversion
  → Illustrator import, scale, trim artboard, 3 mm bleed
  → colour mapping, three-way proof, preflight
  → PDF/X-4 candidate with trim/bleed boxes and Japanese marks
  → Gate 3 structure check + QA report
```

Every step writes JSON evidence. PASS needs evidence. A missing check is
PENDING, never PASS. A candidate PDF is not a final delivery.

---

## Why this exists

Most RGB-to-CMYK damage happens quietly:

- The conversion uses whatever CMYK workspace the operator happens to have.
- A check passes because the script ran, not because the file is correct.
- A stale gate result gets attached to a new PDF.
- A "fix" changes the artwork to satisfy a checker.

This pipeline encodes the opposite rules:

| Rule | Implementation |
|---|---|
| One conversion point | Ghostscript with explicit ICC, intent, BPC in `conversion.json` |
| No colour repair in Illustrator | `02-import-convert.jsx` only handles geometry |
| Evidence over claims | `gate3.json`, `qa.json`, SHA-256 binding for source, PDF, AI, config |
| Honest states | `PASS` / `FAIL` / `PENDING` / `WARN`; `ok:true` only means the script ran |
| Authorised changes stay visible | Single-K and overprint overrides are explicit inputs and stay PENDING |

---

## Requirements

| Component | Version | Needed for |
|---|---|---|
| macOS | 13+ | Illustrator automation |
| Adobe Illustrator | 2024 (28.x) | `.ai` import, QA, geometry |
| Python | 3.11+ | audit and gate scripts |
| Ghostscript | 10.x | the only RGB→CMYK conversion |
| LittleCMS (`tificc`, `transicc`) | `little-cms2` package | ICC Lab / DeltaE00 prediction |
| pypdf, numpy | see `requirements.txt` | PDF inspection, raster checks |
| qpdf | 11+ | structural verification (recommended) |
| MuPDF tools | 1.24+ | visual diff (recommended) |
| ICC profiles | ISO Coated v2 300% (ECI) + sRGB | colour target |
| Proof profiles | Adobe Coated FOGRA39 + sRGB | soft proof, Gate 3 visual checks |

The Python-only steps run on Linux and Windows. The `.jsx` steps and `run.sh`
need macOS with Illustrator.

macOS must allow your terminal (or agent) to control Illustrator:
System Settings → Privacy & Security → Automation. Without it, every
`run.sh` call fails with an AppleEvent error.

This repository **does not ship ICC profiles**. See
[`color/profiles/README.md`](color/profiles/README.md).

---

## Install — manual

```bash
git clone <this-repo> agentic-rgb-cmyk-preprint
cd agentic-rgb-cmyk-preprint

# 1. system tools
brew install ghostscript little-cms2 qpdf mupdf-tools python@3.12

# 2. python deps
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. ICC profiles (either copy existing files or download them)
#    Put them in color/profiles/ and edit config/print-spec.json → print.iccProfile
ls "/Library/Application Support/Adobe/Color/Profiles/Recommended/"

# 4. verify the environment
./install/verify.sh
```

Then, in every shell that runs the pipeline:

```bash
cd agentic-rgb-cmyk-preprint
source .venv/bin/activate
```

`install/verify.sh` checks Illustrator, Ghostscript, Python deps, the ICC file,
config parsing, and tool availability. Add `--full` to also run the negative
preflight fixture. It does not modify files (the fixture writes its result).

### First job

```bash
mkdir -p jobs/2026-01-01-example/source
cp examples/job.example.json jobs/2026-01-01-example/job.json
# put the Figma PDF export in jobs/2026-01-01-example/source/

./scripts/01-prepare.sh 2026-01-01-example
./scripts/run.sh 2026-01-01-example 01b-rgb-audit.jsx
./scripts/run.sh 2026-01-01-example 02-import-convert.jsx
./scripts/run.sh 2026-01-01-example 03-qa.jsx
python3 scripts/05-preflight.py 2026-01-01-example
python3 scripts/06-color-audit.py 2026-01-01-example
python3 scripts/07-proof.py 2026-01-01-example
python3 scripts/07c-finalize-pdf.py 2026-01-01-example
python3 scripts/08-gate3.py 2026-01-01-example
python3 scripts/04-report.py 2026-01-01-example
```

Run all commands from the repository root. Scripts resolve `config/`,
`color/`, and `jobs/` relative to the repo root.

---

## Install — agent

Copy this prompt into an agent with shell access (Codex, Claude Code, or
similar). It installs, verifies, and reports. It does not run a print job.

```text
Install the repository at <path>/agentic-rgb-cmyk-preprint on this macOS machine.
Read README.md, install/verify.sh, and color/profiles/README.md before you start.

Do:
1. Check macOS, Adobe Illustrator 2024/28.x, Ghostscript 10.x, LittleCMS
   (tificc, transicc), Python 3.11+, qpdf, and mupdf. Report versions. Confirm the terminal has Automation
   permission for Illustrator (System Settings → Privacy & Security → Automation).
2. Install missing Homebrew packages: ghostscript little-cms2 qpdf mupdf-tools python@3.12.
3. Create .venv and install requirements.txt.
4. Find a local ISO Coated v2 300% (ECI) ICC and an sRGB ICC. Copy them into
   color/profiles/ and set print.iccProfile in config/print-spec.json.
   If no ICC exists, stop and ask. Do not download ICC files from unknown hosts.
5. Run ./install/verify.sh. Paste the output.
6. Run: source .venv/bin/activate && python3 scripts/09-selftest.py
   Confirm the expected FAIL and report the numbers.

Do not:
- Modify files under scripts/ to make checks pass.
- Change config values other than print.iccProfile.
- Claim PDF/X compliance or print readiness.
- Run a production job.

Report: versions, ICC paths, verify.sh output, selftest result, and any gap.
```

The same prompt ships as [`docs/install-agent.md`](docs/install-agent.md).

---

## Pipeline

| Step | Command | Reads | Writes |
|---|---|---|---|
| 1 | `01-prepare.sh <job>` | `source/` | `cmyk.pdf`, `prepare.json`, `conversion.json` |
| 2 | `run.sh <job> 01b-rgb-audit.jsx` | `source/` | `rgb-audit.json`, `rgb-ai.tif` |
| 3 | `run.sh <job> 02-import-convert.jsx` | `cmyk-final.pdf` or `cmyk.pdf` | `<job>.ai` |
| 4 | `run.sh <job> 03-qa.jsx` | `.ai` | `qa-result.json` |
| 5 | `05-preflight.py <job>` | `cmyk.pdf` | `preflight.json` |
| 6 | `06-color-audit.py <job>` | `cmyk.pdf`, `prepare.json` | `color-audit.json`, `color-mapping.md` |
| 7 | `07-proof.py <job>` | source + cmyk + ai | `proof.json`, comparison PNGs |
| 8 | `07c-finalize-pdf.py <job>` | `cmyk-final.pdf` | candidate PDF |
| 9 | `08-gate3.py <job>` | candidate PDF + `.ai` | `gate3.json` |
| 10 | `04-report.py <job>` | all evidence | `qa.json`, `qa.md`, checklist |

### Two intermediate PDFs

```text
source/*.pdf ──01──> cmyk.pdf ──01d──> cmyk-final.pdf ──02──> <job>.ai
                        │                   │                  │
                     05 / 06              07c                 03
                        └─────── 07 ───────┴─────> 08 ──> 04
```

- `01` writes `cmyk.pdf`.
- `01d` always writes `cmyk-final.pdf` (geometry-only scale to trim + bleed, no second conversion).
- `07c` reads **only** `cmyk-final.pdf`.
- `02` reads `cmyk-final.pdf` when `scaleInPdf` is true, else `cmyk.pdf`.
- `10-gray-to-k.py` changes **only** `cmyk.pdf`. Re-run `01d` after it.

Mixing these boundaries silently drops a colour change from the final PDF.

See [`docs/pipeline.md`](docs/pipeline.md) for inputs, outputs, and rerun rules.

---

## Gates

| Gate | Question | PASS needs |
|---|---|---|
| Gate 1 | Is the source unchanged and correctly interpreted? | source hash, sRGB copy, palette, size, Figma node snapshot |
| Gate 2 | Did the conversion preserve the approved artwork? | CMYK tuples, visible proof, bleed, structure |
| Gate 3 | Is the candidate PDF structurally printable? | OutputIntent, boxes, bleed content, no RGB, fonts, TAC, marks |

Gate 2 PASS does not mean print approval. Gate 3 PASS does not mean the print
shop accepted the file. Soft proof, contract proof, and press conditions remain
external.

Details: [`docs/gates.md`](docs/gates.md).

---

## Conditional changes

These steps are off by default. All of them change the intermediate PDF only,
never `source/`.

| Step | Use when | Guard |
|---|---|---|
| `10-gray-to-k.py` | buyer authorises single-K neutral text | explicit `konly-targets.json`; `color.controlProbe` stays PENDING |
| `11/12-overprint` | printer asks for K overprint on named text | named fills only; `pdf.overprint` stays PENDING when overprint is used |
| `gs -dNOTRANSPARENCY` | one viewer misrenders a transparent group | backup kept; CMYK rounding ≤0.1%; `ai.cmykCounts` stays PENDING |

---

## What this does not do

- It does not design. It does not write to Figma.
- It does not edit text. Figma outlines text before export.
- It does not replace Acrobat preflight, a contract proof, or the print shop.
- It does not ship fonts, ICC profiles, or Adobe assets.
- It does not claim PDF/X. It verifies PDF/X structure and reports the evidence.

Known limits and tested failures: [`docs/limitations.md`](docs/limitations.md).

---

## Layout

```
config/print-spec.json     parameters (single source of truth)
config/marks.json          trim marks
color/brand-map.json       optional colour vocabulary
color/profiles/            your ICC files (not shipped)
scripts/                   pipeline
  lib/                     PDF, ICC, raster, marks, acceptance
docs/                      pipeline, gates, limits, install
examples/job.example.json  job schema
install/                   verify and helper scripts
tests/                     negative fixtures
jobs/                      per-job working directories (git-ignored)
```

---

## Security and privacy

- No credentials are stored in this repository.
- `jobs/` is git-ignored. Job evidence can contain client artwork and names.
- Scripts call Illustrator through AppleScript and write only inside the job
  directory and the repo.
- Review `scripts/run.sh` before you let an agent execute it. It starts
  Illustrator and can replace a job's `.ai` file.

---

## License

MIT. See [`LICENSE`](LICENSE).
