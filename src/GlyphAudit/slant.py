"""Slant/shear math and reference-width matching for the Slant Glyphs panel.

Deliberately free of Glyphs.app / vanilla / AppKit imports so it can be
unit-tested outside Glyphs — the panel (`proof/panel/slant_panel.py`)
adapts these helpers to live GSLayer objects.
"""

from __future__ import annotations

import math
from typing import Optional, Tuple

from GlyphAudit.model import FontView, parse_variant_suffix

# Pivot choices offered by the panel's Origin popup.
PIVOT_BASELINE = "baseline"
PIVOT_XHEIGHT = "x-height ÷ 2"
PIVOT_CAPHEIGHT = "cap-height ÷ 2"
PIVOT_CHOICES = (PIVOT_BASELINE, PIVOT_XHEIGHT, PIVOT_CAPHEIGHT)


def shear_transform(
    angle_deg: float,
    width_pct: float = 100.0,
    height_pct: float = 100.0,
    origin_y: float = 0.0,
) -> Tuple[float, float, float, float, float, float]:
    """6-tuple affine for `GSLayer.applyTransform`: (m11, m12, m21, m22, tX, tY).

        x' = m11·x + m21·y + tX
        y' = m12·x + m22·y + tY

    Slanting is a horizontal shear: m21 = tan(angle) (NOT radians(angle) —
    the skew factor is a ratio, not an angle). Positive angle leans right,
    the italic direction.

    `origin_y` is the vertical pivot: the shear acts on (y − origin_y), so
    points at that height keep their x position. Slanting around the
    baseline (origin_y=0) shifts whole glyphs sideways; pivoting around
    half x-height keeps stems visually centered.

    width_pct / height_pct scale x / y around the coordinate origin,
    composed with the shear: x' = sx·x + tan·sy·(y − origin_y), y' = sy·y.
    """
    tan = math.tan(math.radians(angle_deg))
    sx = width_pct / 100.0
    sy = height_pct / 100.0
    return (
        sx,
        0.0,
        tan * sy,
        sy,
        -tan * sy * origin_y,
        0.0,
    )


def pivot_y(choice: str, x_height: float, cap_height: float) -> float:
    """Resolve an Origin-popup choice to a font-unit y value."""
    if choice == PIVOT_XHEIGHT:
        return x_height / 2.0
    if choice == PIVOT_CAPHEIGHT:
        return cap_height / 2.0
    return 0.0


def ref_advance(ref_view: FontView, glyph_name: str) -> Optional[int]:
    """Reference advance width for `glyph_name`, or None if unmatched.

    Direct name lookup first; on a miss, strip a recognised variant suffix
    (`a.smcp` → `a`) and try the base glyph — mirrors how the comparator
    pairs variants across fonts.
    """
    adv = ref_view.advances.get(glyph_name)
    if adv is not None:
        return adv
    parsed = parse_variant_suffix(glyph_name)
    if parsed:
        base, _feature = parsed
        return ref_view.advances.get(base)
    return None


def scale_advance(value: float, from_upm: int, to_upm: int) -> int:
    """Scale an advance from the reference's UPM into target font units.

    Same-UPM is a rounded passthrough; cross-UPM scales linearly (the
    comparator normalizes to 1000 UPM for comparison — here we need real
    target units to write into `layer.width`).
    """
    if from_upm == to_upm:
        return int(round(value))
    return int(round(value * to_upm / from_upm))
