"""Extrema detection / insertion / removal math for slanted outlines.

Pure Python on ``(x, y)`` tuples — no Glyphs imports — so it is fully
unit-testable outside Glyphs.app. The Slant Glyphs panel
(`proof/panel/slant_panel.py`) adapts these helpers to GSPath/GSNode
objects.

Only **X extrema** are handled: the slant transform is a horizontal shear
(x' = sx·x + tanθ·sy·(y − y₀), y' = sy·y), under which dy/dt roots don't
move — top/bottom extrema are preserved by construction. Only left/right
extrema (the 3 and 9 o'clock points of round glyphs) shift and need
re-inserting.
"""

from __future__ import annotations

import math
from typing import List, Tuple

Point = Tuple[float, float]
Cubic = Tuple[Point, Point, Point, Point]

#: |t| distance from 0/1 within which a root counts as an endpoint
#: extremum (i.e. the existing node already sits on the extreme).
END_TOL = 1e-6

_ROOT_TOL = 1e-9


def _derivative_coeffs(v0: float, v1: float, v2: float, v3: float):
    """Quadratic coefficients (a, b, c) of B'(t) = a·t² + b·t + c for one
    axis of a cubic Bézier."""
    a = 3.0 * (v3 - 3.0 * v2 + 3.0 * v1 - v0)
    b = 6.0 * (v0 - 2.0 * v1 + v2)
    c = 3.0 * (v1 - v0)
    return a, b, c


def _solve_quadratic(a: float, b: float, c: float) -> List[float]:
    if abs(a) < _ROOT_TOL:
        if abs(b) < _ROOT_TOL:
            return []
        return [-c / b]
    disc = b * b - 4.0 * a * c
    if disc < 0:
        return []
    root = math.sqrt(disc)
    return [(-b + root) / (2.0 * a), (-b - root) / (2.0 * a)]


def cubic_x_extrema_ts(cubic: Cubic, end_tol: float = END_TOL) -> List[float]:
    """Interior t values where dx/dt = 0 (vertical tangent), sorted.

    Roots within `end_tol` of the endpoints are excluded — the existing
    node already covers that extreme.
    """
    p0, p1, p2, p3 = cubic
    a, b, c = _derivative_coeffs(p0[0], p1[0], p2[0], p3[0])
    return sorted(
        t for t in _solve_quadratic(a, b, c) if end_tol < t < 1.0 - end_tol
    )


def cubic_point(cubic: Cubic, t: float) -> Point:
    """Point on the cubic at parameter t."""
    p0, p1, p2, p3 = cubic
    mt = 1.0 - t
    w0 = mt * mt * mt
    w1 = 3.0 * mt * mt * t
    w2 = 3.0 * mt * t * t
    w3 = t * t * t
    return (
        w0 * p0[0] + w1 * p1[0] + w2 * p2[0] + w3 * p3[0],
        w0 * p0[1] + w1 * p1[1] + w2 * p2[1] + w3 * p3[1],
    )


def cubic_endpoint_tangents(cubic: Cubic) -> Tuple[Point, Point]:
    """Tangent vectors B'(0) and B'(1) at the segment endpoints."""
    p0, p1, p2, p3 = cubic
    ax, bx, cx = _derivative_coeffs(p0[0], p1[0], p2[0], p3[0])
    ay, by, cy = _derivative_coeffs(p0[1], p1[1], p2[1], p3[1])
    return (cx, cy), (ax + bx + cx, ay + by + cy)


def is_vertical_tangent(tangent: Point, tol: float = 0.02) -> bool:
    """True when the tangent is (near-)vertical — i.e. the point is a
    left/right extremum. `tol` is a slope ratio: |dx| ≤ tol·|dy|, so 0.02
    ≈ 1.1° off vertical. Pre-slant integer outlines have exactly-zero dx;
    the tolerance exists for post-transform float coordinates."""
    dx, dy = tangent
    return abs(dx) <= tol * max(abs(dy), 1e-9)


def subdivide_cubic(cubic: Cubic, t: float) -> Tuple[Cubic, Cubic]:
    """De Casteljau split at t → (first, second) cubics sharing the
    on-curve split point."""
    p0, p1, p2, p3 = cubic

    def lerp(a: Point, b: Point) -> Point:
        return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)

    q0, q1, q2 = lerp(p0, p1), lerp(p1, p2), lerp(p2, p3)
    r0, r1 = lerp(q0, q1), lerp(q1, q2)
    m = lerp(r0, r1)
    return (p0, q0, r0, m), (m, r1, q2, p3)


def subdivide_cubic_multi(cubic: Cubic, ts: List[float]) -> List[Cubic]:
    """Split at multiple ascending t values → len(ts)+1 cubics, chained
    end-to-start. Parameter mapping is adjusted across successive splits.
    """
    pieces: List[Cubic] = []
    rest = cubic
    t_prev = 0.0
    for t in sorted(ts):
        if not 0.0 < t < 1.0:
            continue
        t_local = (t - t_prev) / (1.0 - t_prev)
        first, rest = subdivide_cubic(rest, t_local)
        pieces.append(first)
        t_prev = t
    pieces.append(rest)
    return pieces


def _dist(p: Point, q: Point) -> float:
    return math.hypot(p[0] - q[0], p[1] - q[1])


def point_cubic_distance(cubic: Cubic, p: Point, coarse: int = 32) -> float:
    """Approximate shortest distance from point `p` to the cubic: coarse
    parameter scan + golden-section refinement around the best sample.
    Kept cheap — it runs inside Glyphs on the main thread."""
    def d2(t: float) -> float:
        q = cubic_point(cubic, t)
        return (q[0] - p[0]) ** 2 + (q[1] - p[1]) ** 2

    best_i, best_d = 0, d2(0.0)
    for i in range(1, coarse + 1):
        d = d2(i / coarse)
        if d < best_d:
            best_i, best_d = i, d
    lo = max(0.0, (best_i - 1) / coarse)
    hi = min(1.0, (best_i + 1) / coarse)
    inv_phi = (math.sqrt(5.0) - 1.0) / 2.0
    a, b = lo, hi
    c = b - inv_phi * (b - a)
    d = a + inv_phi * (b - a)
    fc, fd = d2(c), d2(d)
    for _ in range(24):
        if fc < fd:
            b, d, fd = d, c, fc
            c = b - inv_phi * (b - a)
            fc = d2(c)
        else:
            a, c, fc = c, d, fd
            d = a + inv_phi * (b - a)
            fd = d2(d)
    return math.sqrt(min(d2(a), d2(b)))


def merge_segments(seg1: Cubic, seg2: Cubic) -> Cubic:
    """Single cubic approximating two joined cubics after deleting their
    shared on-curve node — "un-subdivide": recover the handles a de
    Casteljau split would have produced, estimating the split parameter
    from control-polygon lengths. Exact when the node really came from a
    subdivision; close to what Glyphs' keep-shape node removal produces.
    """
    a, c1, _c2, _b = seg1
    _b2, _c3, c4, d = seg2

    def polygon_length(seg: Cubic) -> float:
        return sum(_dist(seg[i], seg[i + 1]) for i in range(3))

    l1, l2 = polygon_length(seg1), polygon_length(seg2)
    t = l1 / (l1 + l2) if (l1 + l2) > 0 else 0.5
    t = min(max(t, 0.05), 0.95)
    h1 = (a[0] + (c1[0] - a[0]) / t, a[1] + (c1[1] - a[1]) / t)
    h2 = (d[0] + (c4[0] - d[0]) / (1.0 - t), d[1] + (c4[1] - d[1]) / (1.0 - t))
    return (a, h1, h2, d)


def _unit(vx: float, vy: float):
    length = math.hypot(vx, vy)
    if length < 1e-9:
        return None
    return (vx / length, vy / length)


def segment_point(seg, t: float) -> Point:
    """Point at parameter t on a segment: 2-point tuple (line) or
    4-point tuple (cubic Bézier)."""
    if len(seg) == 2:
        a, b = seg
        return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
    return cubic_point(seg, t)


def fit_merged_segment(seg1, seg2, samples: int = 24):
    """Least-squares merged cubic for two joined segments after deleting
    their shared node, keeping the endpoint tangent DIRECTIONS from the
    outer handles (Schneider-style: directions fixed, lengths solved).

    Segments may be cubics (4 points) or lines (2 points) in any
    combination — for a line side, the "outer handle direction" is the
    line's own direction. This covers the arch-to-stem join, where a
    generic keep-shape refit produces drawn-out degenerate handles.

    Returns (h1, h2, max_deviation) for the merged cubic
    (A, h1, h2, D), or None when an outer handle is degenerate or the
    solve goes backward (negative handle length).
    """
    a, d = seg1[0], seg2[-1]
    dir0 = _unit(seg1[1][0] - a[0], seg1[1][1] - a[1])
    dir1 = _unit(seg2[-2][0] - d[0], seg2[-2][1] - d[1])
    if dir0 is None or dir1 is None:
        return None

    # Sample the exact current shape, chord-length parameterized.
    pts = [segment_point(seg1, i / samples) for i in range(samples + 1)]
    pts += [segment_point(seg2, i / samples) for i in range(1, samples + 1)]
    dists = [0.0]
    for i in range(1, len(pts)):
        dists.append(dists[-1] + _dist(pts[i - 1], pts[i]))
    total = dists[-1]
    if total < 1e-9:
        return None
    ts = [di / total for di in dists]

    # B(t) = C(t) + alpha·w1·dir0 + beta·w2·dir1, solve 2×2 least squares.
    s00 = s01 = s11 = b0 = b1 = 0.0
    for t, p in zip(ts, pts):
        mt = 1.0 - t
        w0 = mt * mt * mt
        w1 = 3.0 * mt * mt * t
        w2 = 3.0 * mt * t * t
        w3 = t * t * t
        cx = a[0] * (w0 + w1) + d[0] * (w2 + w3)
        cy = a[1] * (w0 + w1) + d[1] * (w2 + w3)
        rx, ry = p[0] - cx, p[1] - cy
        s00 += w1 * w1
        s01 += w1 * w2 * (dir0[0] * dir1[0] + dir0[1] * dir1[1])
        s11 += w2 * w2
        b0 += w1 * (dir0[0] * rx + dir0[1] * ry)
        b1 += w2 * (dir1[0] * rx + dir1[1] * ry)
    det = s00 * s11 - s01 * s01
    if abs(det) < 1e-12:
        return None
    alpha = (b0 * s11 - b1 * s01) / det
    beta = (b1 * s00 - b0 * s01) / det
    if alpha < 0 or beta < 0:
        return None
    h1 = (a[0] + alpha * dir0[0], a[1] + alpha * dir0[1])
    h2 = (d[0] + beta * dir1[0], d[1] + beta * dir1[1])
    merged = (a, h1, h2, d)
    dev = max(point_cubic_distance(merged, p) for p in pts)
    return h1, h2, dev


def balance_extremum_handles(
    prev_seg: Cubic,
    next_seg: Cubic,
    ratio_limit: float = 1.4,
    gate: float = 4.0,
    samples: int = 16,
):
    """Harmonize the two handles adjacent to an X-extremum node E shared
    by `prev_seg` (P, p1, p2, E) and `next_seg` (E, e1, e2, N).

    At a true left/right extremum the tangent is vertical, so the
    E-adjacent handles should be vertical (same x as E) and of comparable
    length. When their length ratio exceeds `ratio_limit`, both are
    snapped to E's x and set to a shared length chosen (from a small
    candidate set: extremes, means, quartiles) to minimize deviation from
    the current shape. Returns the adjusted (prev_seg, next_seg), or None
    to leave the segments untouched (ratio within limit, degenerate
    geometry, or deviation over `gate`).
    """
    _p, _p1, p2, e = prev_seg
    _e0, e1, _e2, _n = next_seg
    v1 = (p2[0] - e[0], p2[1] - e[1])
    v2 = (e1[0] - e[0], e1[1] - e[1])
    l1, l2 = math.hypot(*v1), math.hypot(*v2)
    if min(l1, l2) < 1e-6 or max(l1, l2) / min(l1, l2) <= ratio_limit:
        return None
    # Each handle must be predominantly vertical (above/below E) —
    # anything else can't be meaningfully balanced.
    if abs(v1[1]) < 0.5 * l1 or abs(v2[1]) < 0.5 * l2:
        return None
    s1 = 1.0 if v1[1] > 0 else -1.0
    s2 = 1.0 if v2[1] > 0 else -1.0

    pts = [cubic_point(prev_seg, i / samples) for i in range(samples + 1)]
    pts += [cubic_point(next_seg, i / samples) for i in range(1, samples + 1)]

    def deviation(length: float) -> float:
        new_prev = (prev_seg[0], prev_seg[1], (e[0], e[1] + s1 * length), e)
        new_next = (e, (e[0], e[1] + s2 * length), next_seg[2], next_seg[3])
        worst = 0.0
        for p in pts:
            worst = max(
                worst,
                min(
                    point_cubic_distance(new_prev, p),
                    point_cubic_distance(new_next, p),
                ),
            )
        return worst

    lo, hi = min(l1, l2), max(l1, l2)
    # Small candidate set instead of a fine search — this is a cosmetic
    # pass and each deviation() call is expensive Python running on the
    # Glyphs main thread.
    candidates = {
        lo, hi, (lo + hi) / 2.0, math.sqrt(lo * hi),
        lo + 0.25 * (hi - lo), lo + 0.75 * (hi - lo),
    }
    best_l, best_d = min(
        ((length, deviation(length)) for length in candidates),
        key=lambda ld: ld[1],
    )
    if best_d > gate:
        return None
    new_prev = (prev_seg[0], prev_seg[1], (e[0], e[1] + s1 * best_l), e)
    new_next = (e, (e[0], e[1] + s2 * best_l), next_seg[2], next_seg[3])
    return new_prev, new_next


def removal_deviation(seg1: Cubic, seg2: Cubic, samples: int = 24) -> float:
    """Estimated max shape deviation (font units) from deleting the shared
    on-curve node between `seg1` and `seg2` with keep-shape handle refit
    (see `merge_segments`). Used as the shape gate: delete only when this
    stays under ~1 unit.
    """
    merged = merge_segments(seg1, seg2)
    worst = 0.0
    for seg in (seg1, seg2):
        for i in range(1, samples):
            p = cubic_point(seg, i / samples)
            worst = max(worst, point_cubic_distance(merged, p))
    return worst
