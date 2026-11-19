# Colour contract and `/DefaultCMYK` policy audit

Date: 2026-11-19
Baseline: `200250d` (`origin/main`, PR #1 merged)
Branch: `feat/color-contract-hardening`

Scope: colour identity, PDF/X-4 output structure, brand-colour gates, and
generated negative fixtures. No client artwork, signed-off job, or private
Figma identifier is used in this repository.

## What changed

- Added `config/color-contract.json` as the single source of truth for the
  brand colour, the source/target/render ICC profiles, the conversion intent
  and the PDF/X target. It is git-ignored; `config/color-contract.example.json`
  is the template.
- Added `scripts/lib/contract.py`. It validates the contract, resolves the ICC
  files, rejects a stale or disagreeing `config/print-spec.json` mirror, and
  exposes one brand-colour name. No colour stage keeps a fallback tuple or
  profile hash.
- `01-prepare.sh` records the contract SHA-256 and the source ICC hash in
  `work/conversion.json`; a target ICC that changes after the contract load
  stops the step.
- `06-color-audit.py` matches brand colours by exact 8-bit tuple, not by
  nearest neighbour, and records whether the recorded target ICC still matches
  the contract.
- `07c-finalize-pdf.py` writes one GTS_PDFX OutputIntent and **no
  `/DefaultCMYK`**. It writes XMP `pdfxid:GTS_PDFXVersion`, `xmpMM` document and
  instance IDs, `xmp:MetadataDate`, `pdf:Trapped`, and a trailer `/ID`. A base
  PDF that still declares `/DefaultCMYK` is refused.
- `08-gate3.py` adds `pdf.nodefaultcmyk` and `pdf.brandgreen`, rewrites
  `pdf.separationsManaged` to sample declared regions with an explicit output
  ICC and a different-profile control, and binds the acceptance hash to the
  contract, conversion, marks, control ICC and toolchain.
- `scripts/lib/pdfpaint.py` parses fill events and verifies that each declared
  region has exactly one opaque, Normal, non-overprinting, unclipped DeviceCMYK
  fill, and that every greenish operator matches a declared contract colour.
- `scripts/13-rca-selftest.py` now generates all fixtures itself and covers the
  new rules: page and Form `/DefaultCMYK`, stale contract, ICCBased CMYK
  inheritance, duplicate brand fills, undeclared third greens, and a
  region-sampled managed separation.

## Why `/DefaultCMYK` is gone

The previous rule required an ICCBased `/DefaultCMYK` equal to the delivery
profile. The RCA showed that the same stored `k` values can be re-separated by
a reader through that default space. PDF/X-4 does not require
`/DefaultCMYK`: DeviceCMYK under one matching CMYK OutputIntent is the
supported model. The new policy writes one OutputIntent and no default CMYK
remap.

This is an executable policy decision, not a claim that the standard forbids
`/DefaultCMYK`. The gate reports the structure it verifies.

## Verification

```text
$ python3 scripts/13-rca-selftest.py
{"ok": true, "counts": {"checks": 32, "pass": 32, "fail": 0}}

$ python3 scripts/08-gate3.py _selftest --selftest
{"overall": "fail", "counts": {"pass": 17, "fail": 8, "pending": 2}, "selftest": true}

$ python3 scripts/09-selftest.py
{"ok": true, "checks": 3/3}
```

Synthetic finalize check with the example contract:

```text
07c: one /GTS_PDFX OutputIntent, condition FOGRA39, registry http://www.color.org
     no /DefaultCMYK in the final PDF
     no /ArtBox
     XMP PDF/X-4, pdf:Trapped, MetadataDate, DocumentID, InstanceID
     trailer /ID present
08:  pdf.outputintent pass, pdf.nodefaultcmyk pass, pdf.pdfx pass
```

The synthetic job is not a delivery: it has no real marks layout, no approved
AI, and a fake conversion contract hash. Its expected Gate 3 failures are not
evidence of a regression.

## Limits

- All ΔE00 values remain ICC predictions. No contract proof or press
  measurement was made here.
- Printer RIP output space, intent handling, and plate consistency across both
  sides are external. Equal PDFs do not prove equal press sheets.
- The brand-colour gate depends on declared Figma regions. A job without a
  matching region fails instead of passing silently.
- PDF/X-4 is an internal structure self-check. Acrobat Preflight and the print
  shop's written acceptance remain external.
- The contract is per brand/job. Treat its values as production inputs, not as
  defaults for another job.

## Result

PASS for the code and documentation scope. The brand colour has one owner from
the contract through the candidate PDF, and the new gates have executable
negative fixtures.
