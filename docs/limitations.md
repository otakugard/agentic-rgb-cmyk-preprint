# Known limits and tested failure modes

These are measured behaviours, not guesses. Each item lists the symptom and the
handling used by this repository.

## Figma and import

| Symptom | Cause | Handling |
|---|---|---|
| Source PDF is 1.333× too large | Figma writes 1 px as 1 pt | multiply by 0.75 (`figma.pxToPt`) |
| Text cannot be edited in the `.ai` | Figma outlines text as Type3 | change text in Figma and rerun |
| Photos are recompressed | DCTDecode in the Figma PDF | use `rawImages` when lossless pixels matter |
| Imported page becomes one non-native object | placed + embed route | open natively with `app.open()` |
| `item.fills` is always empty | PDF-imported art uses `fillColor` | QA reads `filled` / `fillColor` |
| A job `.ai` suddenly will not open | Illustrator caches absolute paths | unique job-id filename; rename as a fallback |
| Shadow bitmaps are about 192 ppi | Figma rasterises shadows small | usually acceptable; report PENDING |
| Panels disappear after import (measured on 28.4.1) | `/S /Alpha` clip masks read as black luminosity | set `maskPolicy: "whiten-alpha"`; the mask fill becomes white, the clip stays |
| `.ai` still contains Indexed RGB | it is the `/Thumb` thumbnail | page RGB operators are 0; explain the thumbnail |
| Embedded ICC is not visible in the `.ai` | PDF part has no `/ICCBased` | do not claim ICC transfer from the `.ai` |

## Colour and conversion

| Symptom | Cause | Handling |
|---|---|---|
| Cream background shifts in proof | `tificc` reads the ICC embedded by Ghostscript | pass `-n` and an explicit profile |
| TAC looks impossible at 300 dpi | hidden or clipped text remains in the content stream | cross-check 300 dpi visibility; do not call it a visible risk |
| One CMYK tuple disappears | Illustrator omits hidden objects on save | WARN when the visible render is identical |
| Byte scan misses Type3 / SMask | they live in object streams | decode object streams first (`lib/pdfprobe.py`) |
| Control probe deviates after an ICC change | a library constant still points at the old profile | pass the profile explicitly; never rely on a module default |
| DeltaE00 jumps after a single-K override | greedy nearest-match pairing | make the override an explicit input (`konly-targets.json`) |

## Overprint

| Symptom | Cause | Handling |
|---|---|---|
| `gs -sOverprint=simulate` shows no change | unreliable on clipped/artifact PDFs | use `-sDEVICE=tiffsep` plates; 255 = no ink |
| Overprint is correct but the text changes colour | ink under the text is no longer knocked out | predict DeltaE00 and require human acceptance |
| Structural flag is set but nothing prints differently | missing `/op` or `/OPM` on the state | check ExtGState and content selection separately |

## Viewers

| Symptom | Cause | Handling |
|---|---|---|
| macOS Preview shows a cream block over text | PDFKit misreads a transparent/clipped group | verify with Ghostscript and MuPDF first; flatten only for the viewer issue |
| Flattening changes CMYK slightly | 8-bit rounding | accept ≤0.1%; `ai.cmykCounts` becomes PENDING |

## Automation

| Symptom | Cause | Handling |
|---|---|---|
| AppleEvent `-1712` on the first run after idle | default 120 s AppleEvent timeout | probe with `get version`, `name of every document`, `1+1`; rerun; do not force quit |
| `app.open` returns the user's document | Illustrator reuses open documents | record the document list first; stop on a hit; never close the user's document |
| Two jobs collide | Illustrator has one foreground document | one job at a time; `run.sh` takes a lock |
| `pypdf` reports replaced content but the PDF is unchanged | `page.get_contents()` wraps the stream | edit the `/Contents` indirect object, then verify `remainingTargetTuples` |

## Scope

- One page per job. Multi-page sources are rejected.
- No CMYK source. This pipeline converts RGB once; it does not re-separate.
- No automatic correction of colour, geometry, text, or overprint to satisfy a
  check.
