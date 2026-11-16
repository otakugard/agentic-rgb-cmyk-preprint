# Agent install prompt

Copy the block below into an agent with shell access. Replace `<path>` with the
absolute path where the repository should live. The agent installs, verifies,
and reports. It must not run a production job.

The repository already exists at that path; if it does not, clone it first and
then run this prompt.

```text
Install the repository at <path>/agentic-rgb-cmyk-preprint on this macOS machine.
Read README.md, install/verify.sh, and color/profiles/README.md before you start.

Do:
1. Check macOS, Adobe Illustrator 2024 (28.0) or newer, Ghostscript 10.x, LittleCMS
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

## What the agent should check

| Item | Pass condition |
|---|---|
| Illustrator | an `Adobe Illustrator*.app` is found (config `appPath`, else newest in `/Applications`), version `>= 28.0` |
| Ghostscript | `gs --version` is 10.x |
| LittleCMS | `tificc` and `transicc` resolve on `PATH` |
| Python | 3.11 or newer |
| `pypdf`, `numpy` | import succeeds inside `.venv` |
| qpdf | `qpdf --version` succeeds |
| MuPDF | `mutool -v` succeeds (optional) |
| ICC | `config/print-spec.json` → `print.iccProfile` points to an existing file |
| Selftest | `scripts/09-selftest.py` reports `ok: true` and detects the 325% TAC fixture |

## What the agent must not do

- Do not install a cracked or bundled Adobe product.
- Do not fetch ICC files from a random URL. Use a local Adobe/ECI profile or
  ask the user.
- Do not change `print.iccProfile` to a profile you did not verify by name.
- Do not lower `illustrator.minimumVersion`. 2024 (28.0) is the floor; a newer
  release does not need a config change.
- Do not run `01-prepare.sh` or any job step. Installation stops at verification.
- Do not modify `scripts/` to turn a FAIL into a PASS.

## Manual equivalent

```bash
brew install ghostscript little-cms2 qpdf mupdf-tools python@3.12
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
./install/verify.sh
```

## After install

The first real job needs a Figma PDF export and an Illustrator session. Read
[`pipeline.md`](pipeline.md) before running one.
