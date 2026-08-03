"""Unit tests for GlyphAudit.slant — pure shear math + ref-width matching.

These stay Glyphs/AppKit-free; the panel that consumes them
(`proof/panel/slant_panel.py`) is exercised by the L5 manual checklist.
"""

from __future__ import annotations

import math

import pytest

from GlyphAudit.model import FontView
from GlyphAudit.slant import (
    PIVOT_BASELINE,
    PIVOT_CAPHEIGHT,
    PIVOT_XHEIGHT,
    pivot_y,
    ref_advance,
    scale_advance,
    shear_transform,
)


def _view(advances: dict, upm: int = 1000) -> FontView:
    return FontView(
        label="Ref", source="/tmp/ref.ttf", source_kind="ttf",
        upm=upm, advances=advances,
    )


class TestShearTransform:
    def test_zero_angle_is_plain_scale(self):
        m = shear_transform(0.0)
        assert m == pytest.approx((1.0, 0.0, 0.0, 1.0, 0.0, 0.0))

    def test_shear_factor_is_tan_not_radians(self):
        m = shear_transform(10.0)
        assert m[2] == pytest.approx(math.tan(math.radians(10.0)))

    def test_positive_angle_leans_right(self):
        # A point above the baseline moves in +x.
        m = shear_transform(12.0)
        assert m[2] > 0

    def test_pivot_keeps_x_at_origin_height(self):
        origin = 250.0
        m = shear_transform(10.0, origin_y=origin)
        x, y = 100.0, origin
        x_new = m[0] * x + m[2] * y + m[4]
        assert x_new == pytest.approx(x)

    def test_baseline_pivot_shifts_top_right(self):
        m = shear_transform(10.0, origin_y=0.0)
        x_new = m[0] * 100.0 + m[2] * 500.0 + m[4]
        assert x_new == pytest.approx(100.0 + math.tan(math.radians(10.0)) * 500.0)

    def test_width_and_height_scaling(self):
        m = shear_transform(0.0, width_pct=80.0, height_pct=110.0)
        assert m[0] == pytest.approx(0.8)
        assert m[3] == pytest.approx(1.1)
        # Shear composes with vertical scale: tan·sy.
        m = shear_transform(10.0, height_pct=110.0)
        assert m[2] == pytest.approx(math.tan(math.radians(10.0)) * 1.1)

    def test_pivot_compensation_scales_with_height(self):
        origin = 250.0
        m = shear_transform(10.0, height_pct=110.0, origin_y=origin)
        x_new = m[0] * 100.0 + m[2] * origin + m[4]
        assert x_new == pytest.approx(100.0)


class TestPivotY:
    def test_baseline(self):
        assert pivot_y(PIVOT_BASELINE, 500, 700) == 0.0

    def test_xheight_half(self):
        assert pivot_y(PIVOT_XHEIGHT, 500, 700) == 250.0

    def test_capheight_half(self):
        assert pivot_y(PIVOT_CAPHEIGHT, 500, 700) == 350.0

    def test_unknown_choice_falls_back_to_baseline(self):
        assert pivot_y("something-else", 500, 700) == 0.0


class TestRefAdvance:
    def test_direct_hit(self):
        view = _view({"a": 500})
        assert ref_advance(view, "a") == 500

    def test_variant_suffix_falls_back_to_base(self):
        view = _view({"a": 500})
        assert ref_advance(view, "a.smcp") == 500

    def test_miss_returns_none(self):
        view = _view({"a": 500})
        assert ref_advance(view, "b") is None

    def test_unknown_suffix_not_stripped(self):
        # `.alt` is not a recognised feature suffix — no base fallback.
        view = _view({"a": 500})
        assert ref_advance(view, "a.alt") is None


class TestScaleAdvance:
    def test_same_upm_passthrough(self):
        assert scale_advance(500.4, 1000, 1000) == 500

    def test_scales_down(self):
        assert scale_advance(2048, 2048, 1000) == 1000

    def test_scales_up(self):
        assert scale_advance(500, 1000, 2048) == 1024

    def test_rounds_to_int(self):
        assert scale_advance(501, 2048, 1000) == 245  # 244.628…
