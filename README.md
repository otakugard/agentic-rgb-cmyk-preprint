# agentic-rgb-cmyk-preprint

An evidence-first prepress pipeline that turns an RGB PDF into a CMYK
Illustrator file and a PDF/X-4 candidate, with machine-checkable gates.

It accepts two entries: **an existing sRGB PDF at final size**, or **a Figma
PDF export**. Both use Ghostscript as the only colour conversion point and
record every change as an explicit input.

```
existing sRGB PDF at final size ─┐
                                 ├→ colour audit + Ghostscript CMYK conversion
Figma PDF export (sRGB, 1:0.75) ─┘
  → Illustrator import, trim artboard, 3 mm bleed
  → colour mapping, three-way proof, preflight
  → PDF/X-4 candidate with trim/bleed boxes and Japanese marks
  → Gate 3 structure check + QA report
```

Every step writes JSON evidence. PASS needs evidence. A missing check is
PENDING. A candidate PDF stays a candidate: delivery needs Gate 3 PASS and the
print shop's written approval.

---

## Why this exists

Most RGB-to-CMYK damage happens quietly:

- The conversion uses whatever CMYK workspace the operator happens to have.
- A check passes because the script ran. The file can still be wrong.
- A stale gate result gets attached to a new PDF.
- A "fix" changes the artwork to satisfy a checker.

This pipeline encodes the opposite rules:

| Rule | Implementation |
|---|---|
| One colour owner | brand tuple, ICC paths and PDF/X target live in `config/color-contract.json` |
| One conversion point | Ghostscript with explicit ICC, intent, BPC in `conversion.json` |
| No colour repair in Illustrator | `02-import-convert.jsx` only handles geometry |
| Evidence over claims | `gate3.json`, `qa.json`, SHA-256 binding for source, PDF, AI, config |
| Honest states | `PASS` / `FAIL` / `PENDING` / `WARN`; `ok:true` reports script completion, acceptance is separate |
| Authorised changes stay visible | Single-K and overprint overrides are explicit inputs and stay PENDING |

---

## Requirements

| Component | Version | Needed for |
|---|---|---|
| macOS | 13+ | Illustrator automation |
| Adobe Illustrator | 2024 (28.0) or newer | `.ai` import, QA, geometry |
| Python | 3.11+ | audit and gate scripts |
| Ghostscript | 10.x | the only RGB→CMYK conversion |
| LittleCMS (`tificc`, `transicc`) | `little-cms2` package | ICC Lab / DeltaE00 prediction |
| pypdf, numpy | see `requirements.txt` | PDF inspection, raster checks |
| qpdf | 11+ | structural verification (recommended) |
| MuPDF tools | 1.24+ | independent render check; missing it leaves the check PENDING |
| ICC profiles | ISO Coated v2 300% (ECI) + sRGB | colour target |
| Colour contract | `config/color-contract.json` | one canonical brand colour and its regions; every colour stage reads it |
| Proof profiles | sRGB | soft proof, Gate 3 visual checks |

The Python-only steps run on Linux and Windows. The `.jsx` steps and `run.sh`
need macOS with Illustrator 2024 (28.0) or newer. Newer releases work as they
are; the version check accepts 2024 (28.0) and newer. The failure modes in
`docs/limitations.md` were measured on 28.4.1.

`config/print-spec.json` → `illustrator.appPath` selects the application. Leave
it empty and `run.sh` uses the newest `/Applications/Adobe Illustrator*/Adobe
Illustrator.app`; set it to pin one release.

macOS must allow your terminal (or agent) to control Illustrator:
System Settings → Privacy & Security → Automation. Without it, every
`run.sh` call fails with an AppleEvent error.

You supply the ICC profiles. See
[`color/profiles/README.md`](color/profiles/README.md).

---

## Install (manual)

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
#    Put them in color/profiles/; point config/color-contract.json → profiles at them
ls "/Library/Application Support/Adobe/Color/Profiles/Recommended/"

# 4. colour contract (one per brand/job; never commit client values)
cp config/color-contract.example.json config/color-contract.json
$EDITOR config/color-contract.json   # brand hex, CMYK tuple, regions, ICC hashes

# 5. verify the environment
./install/verify.sh
```

Then, in every shell that runs the pipeline:

```bash
cd agentic-rgb-cmyk-preprint
source .venv/bin/activate
```

`install/verify.sh` checks Illustrator, Ghostscript, Python deps, the ICC files,
the colour contract, config parsing, and tool availability. Add `--full` to also
run the negative preflight fixture, which writes only its result file.

### One colour contract

`config/color-contract.json` is the only place that defines the brand colour.
Conversion, audit, finalize, Gate 3 and the report read it; the scripts carry no
fallback tuple or profile hash. The file is git-ignored because it can contain
client values. Start from `config/color-contract.example.json`.

```jsonc
{
  "brandColor": "BRAND_GREEN",
  "colors": { "BRAND_GREEN": { "figma": {"rgbHex": "#A2BC97"},
                               "cmyk": {"percent": [40.0, 10.6, 45.1, 5.1],
                                        "bytes8": [102, 27, 115, 13]},
                               "regions": [ /* Figma node -> paint node */ ] } },
  "profiles": { "source": {}, "target": {}, "renderRGB": {} }
}
```

A contract that names another target ICC, another source profile, or a
different brand tuple is a hard error, not a warning. `08-gate3.py` fails when a
declared region is missing, duplicated, or covered by an undeclared green.

### First job: an existing sRGB PDF

Use this entry when the layout is already final and the PDF is at the real
physical size. The source page is trim plus bleed: A3 `420 × 297 mm` with
`3 mm` bleed is `426 × 303 mm`. Do not pre-scale it to the Figma `0.75`
factor.

```bash
mkdir -p jobs/2026-01-02-a3-fold-srgb/source
cp examples/job.srgb-pdf.example.json jobs/2026-01-02-a3-fold-srgb/job.json
# put the sRGB PDF at jobs/2026-01-02-a3-fold-srgb/source/a3-fold-srgb.pdf

./scripts/01-prepare.sh 2026-01-02-a3-fold-srgb
./scripts/run.sh 2026-01-02-a3-fold-srgb 01b-rgb-audit.jsx
./scripts/run.sh 2026-01-02-a3-fold-srgb 02-import-convert.jsx
./scripts/run.sh 2026-01-02-a3-fold-srgb 03-qa.jsx
python3 scripts/05-preflight.py 2026-01-02-a3-fold-srgb
python3 scripts/06-color-audit.py 2026-01-02-a3-fold-srgb
python3 scripts/07-proof.py 2026-01-02-a3-fold-srgb
python3 scripts/07c-finalize-pdf.py 2026-01-02-a3-fold-srgb
python3 scripts/08-gate3.py 2026-01-02-a3-fold-srgb
python3 scripts/04-report.py 2026-01-02-a3-fold-srgb
```

### First job: a Figma export

Use this entry when the layout comes from Figma. The source page is the node
export at Figma's `1 px = 1 pt` size; `01d` scales it by `figma.pxToPt = 0.75`.
The source page size check expects that factor, not the final millimetre size.

```bash
mkdir -p jobs/2026-01-01-example/source
cp examples/job.example.json jobs/2026-01-01-example/job.json
# put the Figma PDF export at jobs/2026-01-01-example/source/0-0.pdf

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

Both entries end with the same candidate and gate chain. A candidate PDF stays
a candidate: delivery needs Gate 3 PASS and the print shop's written approval.


Run all commands from the repository root. Scripts resolve `config/`,
`color/`, and `jobs/` relative to the repo root.

---

## Install (agent)

An agent with shell access can install, verify, and report. The prompt, the pass
conditions, and the prohibitions live in
[`docs/install-agent.md`](docs/install-agent.md). The agent installs the tools and
runs the self test; it stops before any print job.

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
| Gate 1 | Is the source unchanged and correctly interpreted? | source hash, sRGB copy, palette, size; Figma node snapshot or direct-PDF source evidence |
| Gate 2 | Did the conversion preserve the approved artwork? | CMYK tuples, visible proof, bleed, structure |
| Gate 3 | Is the candidate PDF structurally printable? | OutputIntent, boxes, bleed content, no RGB, fonts, TAC, marks, no `/DefaultCMYK`, brand-colour regions, managed separations, independent render, optional fold |

Gate 2 PASS covers conversion integrity; approval is the print shop's decision.
Gate 3 PASS covers output structure; shop acceptance, contract proof, and press
conditions stay external.

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

## Scope

- Design work and Figma writes belong to Figma. This pipeline reads the export.
- Text arrives outlined. Edit it in Figma and re-export.
- Acrobat Preflight, the contract proof, and the press run are separate checks.
- Fonts, ICC profiles, and Adobe assets come from your machine.
- PDF/X: the pipeline verifies the structure and reports the evidence. The print
  shop accepts the file.

Known limits and tested failures: [`docs/limitations.md`](docs/limitations.md).

---

## Layout

```
config/print-spec.json     print parameters (mirror; colour identity comes from the contract)
config/color-contract.example.json  template for config/color-contract.json
config/marks.json          trim marks
color/brand-map.json       optional colour vocabulary
color/profiles/            your ICC files (not shipped)
scripts/                   pipeline
  lib/                     PDF, ICC, raster, marks, acceptance
docs/                      pipeline, gates, limits, install, writing rules
examples/job.example.json  job schema
install/                   verify and helper scripts
tests/                     negative fixtures
jobs/                      per-job working directories (git-ignored)
```

---

## Security and privacy

- Scripts drive Illustrator through AppleScript. They write only inside the job
  directory and the repository.
- `scripts/run.sh` starts Illustrator and can replace a job's `.ai` file. Approve
  it before an agent runs it.

---

## License

MIT, © 2026 The agentic-rgb-cmyk-preprint authors. SPDX identifier: `MIT`.
Full text in [`LICENSE`](LICENSE).

This repository redistributes no third-party binaries, ICC profiles, or fonts.
Ghostscript and LittleCMS run as separate processes. qpdf and MuPDF are optional
manual checks.

| Tool | Role | Licence |
|---|---|---|
| Ghostscript 10.x | the only RGB to CMYK conversion | AGPL-3.0-or-later |
| LittleCMS (`tificc`, `transicc`) | ICC Lab and DeltaE00 prediction | MIT |
| pypdf | PDF inspection | BSD-3-Clause |
| numpy | raster checks | BSD-3-Clause, plus 0BSD, MIT, Zlib, CC0-1.0 in bundled parts |
| qpdf | structural check, run by hand | Apache-2.0 |
| MuPDF (`mutool`) | visual diff, run by hand | AGPL-3.0-or-later |
| Adobe Illustrator | import, QA, geometry | proprietary; you supply the licence |

Ghostscript and MuPDF carry AGPL-3.0-or-later. Ghostscript runs through
`subprocess`; MuPDF stays a manual tool. The pipeline links no library code from
either. A product that bundles their binaries needs its own licence review.

Licence names come from package metadata on the release-test machine:
Ghostscript 10.08.0, LittleCMS 2.20, qpdf 12.4.2, MuPDF 1.28.6, pypdf 6.21.4,
numpy 2.5.4.
