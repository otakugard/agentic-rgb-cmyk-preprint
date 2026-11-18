#!/usr/bin/env python3
"""Source-entry helpers shared by the print pipeline.

Two entries produce the same RGB PDF contract:

  figma      A PDF exported from Figma. Its page is 1 / pxToPt times the
             physical size.
  srgb-pdf   An already laid-out sRGB PDF at the final physical size.

Everything downstream still converts RGB to CMYK once in 01-prepare.sh.
"""


def source_kind(job):
    source = (job or {}).get("source") or {}
    kind = str(source.get("kind") or "figma").strip().lower()
    return kind


def is_direct_pdf(job):
    return source_kind(job) == "srgb-pdf"


def page_scale(job, spec):
    """Factor from the source PDF page to the final bleed page."""
    if is_direct_pdf(job):
        return 1.0
    return float((spec.get("figma") or {}).get("pxToPt", 0.75))


def source_label(job):
    return "an existing sRGB PDF" if is_direct_pdf(job) else "a Figma PDF export"


def validate_kind(job):
    kind = source_kind(job)
    if kind not in ("figma", "srgb-pdf"):
        raise ValueError("source.kind must be 'figma' or 'srgb-pdf' (got %r)" % kind)
    return kind
