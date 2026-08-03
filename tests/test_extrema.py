"""Unit tests for GlyphAudit.extrema — pure cubic-extrema math."""

from __future__ import annotations

import math

import pytest

from GlyphAudit.extrema import (
    cubic_endpoint_tangents,
    cubic_point,
    cubic_x_extrema_ts,
    is_vertical_tangent,
    removal_deviation,
    subdivide_cubic,
    subdivide_cubic_multi,
)
from GlyphAudit.slant import shear_transform

# Quarter-circle arc approximation (kappa = 4/3·(√2−1) ≈ 0.5523), from the
# rightmost point (3 o'clock) to the top (12 o'clock).
KAPPA = 4.0 / 3.0 * (math.sqrt(2.0) - 1.0)
QUARTER_ARC = (
    (100.0, 0.0),
    (100.0, 100.0 * KAPPA),
    (100.0 * KAPPA, 100.0),
    (0.0, 100.0),
)


def _shear_points(cubic, angle):
    m = shear_transform(angle)
    out = []
    for x, y in cubic:
        out.append((m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]))
    return tuple(out)


class TestCubicXExtrema:
    def test_unslanted_arc_has_no_interior_x_extremum(self):
        # x decreases monotonically along the quarter arc.
        assert cubic_x_extrema_ts(QUARTER_ARC) == []

    def test_sheared_arc_gains_interior_x_extremum(self):
        sheared = _shear_points(QUARTER_ARC, 12.0)
        ts = cubic_x_extrema_ts(sheared)
        assert len(ts) == 1
        # The found t must sit at the numeric argmax of x' along the curve:
        # an exact extremum beats every sample of a dense scan.
        t_star = ts[0]
        samples = [(i / 2000.0, cubic_point(sheared, i / 2000.0)) for i in range(2001)]
        t_numeric, (x_numeric, _y) = max(samples, key=lambda s: s[1][0])
        assert t_star == pytest.approx(t_numeric, abs=1e-3)
        assert cubic_point(sheared, t_star)[0] >= x_numeric - 1e-9

    def test_endpoint_roots_excluded(self):
        # A curve whose only x-extremum is at t=1 yields no interior roots.
        seg = ((0.0, 0.0), (50.0, 0.0), (80.0, 0.0), (100.0, 0.0))
        assert cubic_x_extrema_ts(seg) == []

    def test_two_extrema_detected(self):
        # S-curve with a clear max and min in x.
        seg = ((0.0, 0.0), (150.0, 0.0), (-50.0, 100.0), (100.0, 100.0))
        ts = cubic_x_extrema_ts(seg)
        assert len(ts) == 2
        assert ts[0] < ts[1]


class TestEndpointTangents:
    def test_arc_start_is_vertical_tangent(self):
        t0, t1 = cubic_endpoint_tangents(QUARTER_ARC)
        assert is_vertical_tangent(t0, tol=0.0)
        assert not is_vertical_tangent(t1, tol=0.0)

    def test_sheared_arc_start_no_longer_vertical(self):
        sheared = _shear_points(QUARTER_ARC, 12.0)
        t0, _t1 = cubic_endpoint_tangents(sheared)
        # The old 3-o'clock extremum's tangent tilts after shearing.
        assert not is_vertical_tangent(t0)

    def test_tolerance_accepts_near_vertical(self):
        assert is_vertical_tangent((0.01, 1.0), tol=0.02)
        assert not is_vertical_tangent((0.05, 1.0), tol=0.02)


class TestSubdivide:
    def test_split_point_lies_on_curve(self):
        t = 0.37
        first, second = subdivide_cubic(QUARTER_ARC, t)
        assert first[3] == second[0]
        assert first[3] == pytest.approx(cubic_point(QUARTER_ARC, t))

    def test_halves_reproduce_original(self):
        t = 0.62
        first, second = subdivide_cubic(QUARTER_ARC, t)
        worst = 0.0
        for i in range(21):
            s = i / 20.0
            original = cubic_point(QUARTER_ARC, s)
            if s <= t:
                p = cubic_point(first, s / t)
            else:
                p = cubic_point(second, (s - t) / (1.0 - t))
            worst = max(worst, math.hypot(original[0] - p[0], original[1] - p[1]))
        assert worst < 1e-6


class TestSubdivideMulti:
    def test_pieces_chain(self):
        pieces = subdivide_cubic_multi(QUARTER_ARC, [0.3, 0.7])
        assert len(pieces) == 3
        assert pieces[0][0] == QUARTER_ARC[0]
        assert pieces[-1][3] == QUARTER_ARC[3]
        for a, b in zip(pieces, pieces[1:]):
            assert a[3] == b[0]

    def test_split_points_match_original(self):
        pieces = subdivide_cubic_multi(QUARTER_ARC, [0.3, 0.7])
        assert pieces[0][3] == pytest.approx(cubic_point(QUARTER_ARC, 0.3))
        assert pieces[1][3] == pytest.approx(cubic_point(QUARTER_ARC, 0.7))

    def test_unsorted_input_is_sorted(self):
        assert subdivide_cubic_multi(QUARTER_ARC, [0.7, 0.3]) == \
            subdivide_cubic_multi(QUARTER_ARC, [0.3, 0.7])

    def test_empty_ts_returns_original(self):
        assert subdivide_cubic_multi(QUARTER_ARC, []) == [QUARTER_ARC]


class TestFitMergedSegment:
    def test_recovers_original_after_subdivide(self):
        from GlyphAudit.extrema import fit_merged_segment
        first, second = subdivide_cubic(QUARTER_ARC, 0.4)
        result = fit_merged_segment(first, second)
        assert result is not None
        h1, h2, dev = result
        # The fit must reproduce the original handles closely.
        assert h1 == pytest.approx(QUARTER_ARC[1], abs=1.0)
        assert h2 == pytest.approx(QUARTER_ARC[2], abs=1.0)
        assert dev < 0.5

    def test_degenerate_direction_returns_none(self):
        from GlyphAudit.extrema import fit_merged_segment
        # Outer handle coincides with its on-curve point → no direction.
        seg1 = ((0.0, 0.0), (0.0, 0.0), (10.0, 10.0), (50.0, 0.0))
        seg2 = ((50.0, 0.0), (60.0, 10.0), (90.0, 10.0), (100.0, 0.0))
        assert fit_merged_segment(seg1, seg2) is None

    def test_cubic_plus_line(self):
        from GlyphAudit.extrema import fit_merged_segment
        # Arch-into-stem analog: cubic bump + straight continuation.
        seg1 = ((0.0, 0.0), (30.0, 20.0), (70.0, 20.0), (100.0, 0.0))
        seg2 = ((100.0, 0.0), (130.0, -20.0))
        result = fit_merged_segment(seg1, seg2)
        assert result is not None
        h1, h2, dev = result
        assert dev < 3.0
        # The D-side handle must lie exactly on the line's direction.
        d = seg2[-1]
        line_dir = (seg2[1][0] - seg2[0][0], seg2[1][1] - seg2[0][1])
        cross = (h2[0] - d[0]) * line_dir[1] - (h2[1] - d[1]) * line_dir[0]
        assert cross == pytest.approx(0.0, abs=1e-6)

    def test_line_plus_steep_cubic_gates_out(self):
        from GlyphAudit.extrema import fit_merged_segment
        # A line merging into a steep curve is inherently lossy — the
        # reported deviation must be large so the caller keeps the node.
        seg1 = ((0.0, 0.0), (100.0, 0.0))
        seg2 = ((100.0, 0.0), (130.0, -20.0), (170.0, -20.0), (200.0, 0.0))
        result = fit_merged_segment(seg1, seg2)
        assert result is not None
        assert result[2] > 5.0


class TestBalanceExtremumHandles:
    def _arc_pair_around_extremum(self):
        # Two segments meeting at a right-side extremum E=(100,0) with
        # deliberately unequal E-adjacent VERTICAL handles (30 vs 50).
        e = (100.0, 0.0)
        prev = ((0.0, -50.0), (45.0, -50.0), (100.0, -30.0), e)
        nxt = (e, (100.0, 50.0), (55.0, 55.0), (0.0, 50.0))
        return prev, nxt

    def test_unequal_handles_are_equalised(self):
        from GlyphAudit.extrema import balance_extremum_handles
        prev, nxt = self._arc_pair_around_extremum()
        result = balance_extremum_handles(prev, nxt, ratio_limit=1.4)
        assert result is not None
        new_prev, new_next = result
        e = (100.0, 0.0)
        l1 = math.dist(new_prev[2], e)
        l2 = math.dist(new_next[1], e)
        assert l1 == pytest.approx(l2)
        # Handles snapped to E's x (vertical tangent at an x-extremum).
        assert new_prev[2][0] == e[0]
        assert new_next[1][0] == e[0]
        # Best shared length sits between the two originals (30, 50).
        assert 30.0 <= l1 <= 50.0

    def test_within_ratio_limit_returns_none(self):
        from GlyphAudit.extrema import balance_extremum_handles
        prev, nxt = self._arc_pair_around_extremum()
        # Equal-ish E-adjacent handles → untouched.
        prev = (prev[0], prev[1], (100.0, -40.0), prev[3])
        nxt = (nxt[0], (100.0, 44.0), nxt[2], nxt[3])
        assert balance_extremum_handles(prev, nxt, ratio_limit=1.4) is None

    def test_gate_rejects_extreme_changes(self):
        from GlyphAudit.extrema import balance_extremum_handles
        prev, nxt = self._arc_pair_around_extremum()
        # A zero deviation budget must refuse any change.
        assert balance_extremum_handles(prev, nxt, ratio_limit=1.4, gate=0.0) is None

    def test_horizontal_handles_are_left_alone(self):
        from GlyphAudit.extrema import balance_extremum_handles
        e = (100.0, 0.0)
        # Handles pointing sideways instead of up/down — not an x-extremum
        # configuration; must not be "balanced".
        prev = ((0.0, -50.0), (45.0, -50.0), (70.0, 0.0), e)
        nxt = (e, (160.0, 0.0), (55.0, 55.0), (0.0, 50.0))
        assert balance_extremum_handles(prev, nxt, ratio_limit=1.4) is None
class TestRemovalDeviation:
    def test_straight_line_removal_is_exact(self):
        seg1 = ((0.0, 0.0), (10.0, 0.0), (20.0, 0.0), (30.0, 0.0))
        seg2 = ((30.0, 0.0), (40.0, 0.0), (50.0, 0.0), (60.0, 0.0))
        assert removal_deviation(seg1, seg2) == pytest.approx(0.0, abs=1e-4)

    def test_extremum_removal_deviation_is_small(self):
        # Split the arc at its 3-o'clock extremum region: removing that
        # node shifts the outline by well under a unit.
        first, second = subdivide_cubic(QUARTER_ARC, 0.05)
        assert removal_deviation(first, second) < 1.0

    def test_bad_merge_reports_large_deviation(self):
        # Merging two segments with a sharp direction change must be caught
        # by the gate.
        seg1 = ((0.0, 0.0), (30.0, 10.0), (60.0, 20.0), (100.0, 0.0))
        seg2 = ((100.0, 0.0), (60.0, 80.0), (30.0, 90.0), (0.0, 100.0))
        assert removal_deviation(seg1, seg2) > 5.0
