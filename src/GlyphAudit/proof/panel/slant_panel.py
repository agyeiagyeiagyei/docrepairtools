#MenuTitle: Slant Glyphs
# -*- coding: utf-8 -*-
"""Glyphs.app panel for slanting selected glyphs with a preview/tweak loop,
then matching advance widths to the docrepair reference font.

Inspired by filipenegrao's `slant_glyphs.py`
(github.com/filipenegrao/glyphsapp-scripts), reworked for the DocRepair
workflow:

  - Preview/tweak: the first Preview snapshots the selected glyphs'
    outlines and (when the background is empty) copies the originals into
    the background layer as a visual reference. Every re-preview restores
    from the snapshot before applying, so iterations never compound
    transforms. Revert (or closing the window while previewing) restores
    the snapshots byte-for-byte.
  - In-panel compare: a preview pane renders the slanted result of the
    current glyph (blue) against the reference font's matching glyph
    (red), live as you edit fields or change the selection. Vertical
    hairlines mark both advances.
  - Slant math: horizontal shear by tan(angle) around a configurable
    pivot (baseline / x-height÷2 / cap-height÷2), plus optional width and
    height scaling. Pivoting around half x-height keeps stems visually
    centered instead of shifting glyphs sideways.
  - Reference picker: "Auto" resolves each master against its
    `[instances.*]` pin in ~/.glyph-audit/config.toml (the same pins the
    Glyph Audit panel uses and persists); recent files are shared with
    that panel too, so whatever you picked there is already here. Picking
    a specific file/config entry forces it for all masters in scope.
  - Width matching: on Apply, each glyph's advance width is set from the
    resolved reference. Glyphs missing from the reference keep their
    width and are reported.
  - Scope: all masters by default so interpolation stays compatible.
    Components are decomposed before slanting by default.
  - Extrema fixing (default on): before slanting, true X-extremum nodes
    are recorded; after slanting, new extrema are inserted by subdividing
    at the curve's post-transform vertical-tangent points (only left/right
    extrema move under a horizontal shear — top/bottom are preserved).
    Stale pre-slant nodes BETWEEN TWO CURVES are merged out with a
    least-squares handle fit against the exact pre-deletion shape (NOT
    Glyphs' keep-shape refit, which stretches handles), gated on a
    ~1-unit fit deviation. Stale nodes at curve-to-LINE joins (arch to
    stem) are kept, not merged: covering a curve plus a straight stem
    with one cubic is inherently lossy, and Glyphs only manages it via
    a degenerate stem-long handle. The handle pairs adjacent to each new
    extremum are harmonized: snapped vertical and equalized in length
    when their ratio exceeds 1.4, within a ~4-unit deviation budget.
    Glyphs whose node counts end up differing across masters are flagged
    in the summary.

Width matching keeps the left sidebearing: setting `layer.width` in Glyphs
adjusts the RSB to reach the target advance.

Toggle: run the menu item again to close. Closing while a preview is
active reverts the preview.

Install
-------
    pip install docrepair-tools
    glyph-audit proof panel install
"""

import sys
import traceback
from pathlib import Path

# sys.path bootstrap — inline because Glyphs.app runs a symlinked script
# as a standalone module (no `__package__`), so relative imports blow up
# with ImportError. Walk up from this file until we hit a directory with
# `GlyphAudit/__init__.py` and prepend it. No-op when already importable.
_HERE_INIT = Path(__file__).resolve()
if "GlyphAudit" not in sys.modules:
    for _depth in range(1, min(7, len(_HERE_INIT.parents))):
        _root = _HERE_INIT.parents[_depth]
        if (_root / "GlyphAudit" / "__init__.py").exists():
            if str(_root) not in sys.path:
                sys.path.insert(0, str(_root))
            break

# Purge cached GlyphAudit modules so re-running the menu item picks up
# code changes without restarting Glyphs — see audit_panel.py for the
# full rationale.
for _mod in [m for m in sys.modules if m == "GlyphAudit" or m.startswith("GlyphAudit.")]:
    del sys.modules[_mod]

import vanilla
from GlyphsApp import Glyphs, UPDATEINTERFACE

from GlyphAudit.model import parse_variant_suffix
from GlyphAudit.extrema import (
    balance_extremum_handles,
    cubic_endpoint_tangents,
    cubic_point,
    cubic_x_extrema_ts,
    fit_merged_segment,
    is_vertical_tangent,
    subdivide_cubic_multi,
)
from GlyphAudit.proof.panel.audit_common import (
    load_audit_references,
    load_recent_files,
    load_reference_cached,
    open_config_in_editor,
    pick_font_file_async,
    push_recent_file,
    ttfont_for,
)
from GlyphAudit.slant import (
    PIVOT_CHOICES,
    PIVOT_XHEIGHT,
    pivot_y,
    ref_advance,
    scale_advance,
    shear_transform,
)


# The open-panel registry lives on `builtins`, NOT as a class attribute —
# the module-purge above means each menu-item run gets a fresh class, so a
# class-level `_instance` would always be None and toggle-to-close would
# break. `builtins` survives purges. See audit_panel.py.
import builtins as _builtins


def _panel_registry() -> dict:
    reg = getattr(_builtins, "_glyphaudit_panel_registry", None)
    if reg is None:
        reg = {}
        _builtins._glyphaudit_panel_registry = reg
    return reg


# ---------------------------------------------------------------------------
# GSLayer helpers — small compat shims around API differences we can't
# exercise outside Glyphs.app. Each prefers the Glyphs 3 spelling and falls
# back to older forms.
# ---------------------------------------------------------------------------

def _clear_shapes(layer) -> None:
    """Remove all paths/components from a layer (used for backgrounds)."""
    try:
        layer.shapes = []  # Glyphs 3 unified shapes list
        return
    except Exception:
        pass
    for coll_name in ("paths", "components"):
        try:
            coll = getattr(layer, coll_name)
            for item in list(coll):
                coll.remove(item)
        except Exception:
            traceback.print_exc()


def _copy_shapes(src_layer, dst_layer) -> None:
    """Deep-copy all paths/components from one layer into another."""
    copied = False
    try:
        for shape in src_layer.shapes:  # Glyphs 3
            dst_layer.shapes.append(shape.copy())
            copied = True
    except Exception:
        copied = False
    if copied:
        return
    for p in src_layer.paths:
        dst_layer.paths.append(p.copy())
    for c in src_layer.components:
        dst_layer.components.append(c.copy())


def _layer_is_empty(layer) -> bool:
    try:
        return len(layer.shapes) == 0  # Glyphs 3
    except Exception:
        return len(layer.paths) == 0 and len(layer.components) == 0


def _decompose_components(layer) -> None:
    try:
        layer.decomposeComponents()  # Glyphs 3
        return
    except AttributeError:
        pass
    for comp in list(layer.components):
        try:
            comp.decompose()
        except Exception:
            traceback.print_exc()


# ---------------------------------------------------------------------------
# Extrema helpers — GSPath/GSNode bookkeeping. The math lives in
# GlyphAudit.extrema (pure, pytest-covered); verified selectors against the
# GlyphsCore binary: insertNodeWithPathTime:, removeNodeCheckKeepShape:.
#
# Only X extrema are fixed: under the horizontal shear of the slant,
# dy/dt roots don't move, so top/bottom extrema are preserved and only
# left/right (3/9 o'clock) points drift off the true extreme.
# ---------------------------------------------------------------------------

#: Deviation budget (font units) for the least-squares merge fit when
#: removing a stale extremum node between two cubics. Above this the old
#: node is kept and reported. (Measured 0.19 on a test circle.)
_EXTREMA_GATE = 1.0

#: Position-match tolerance when re-finding recorded extremum nodes after
#: the transform. Covers integer-grid rounding (grid spacing ≤ 1).
_POS_TOL = 0.75


def _node_pos(node):
    p = node.position
    return (float(p.x), float(p.y))


def _set_node_pos(node, pt):
    try:
        node.position = (float(pt[0]), float(pt[1]))
    except Exception:
        from Foundation import NSMakePoint
        node.setPosition_(NSMakePoint(float(pt[0]), float(pt[1])))


def _path_cubic_segments(nodes, closed):
    """Cubic segments of a path as 4-node tuples (on, off, off, on),
    wrapping around for closed paths. Works on a single snapshot of the
    node list so proxy identity (`is`) is usable for lookups."""
    oncurve = [i for i, nd in enumerate(nodes) if str(nd.type) != "offcurve"]
    if not oncurve:
        return []
    pairs = list(zip(oncurve, oncurve[1:]))
    if closed and len(oncurve) > 1:
        pairs.append((oncurve[-1], oncurve[0]))
    segments = []
    for i, j in pairs:
        between = nodes[i + 1:j] if j > i else nodes[i + 1:] + nodes[:j]
        if len(between) == 2:
            segments.append((nodes[i], between[0], between[1], nodes[j]))
    return segments


def _path_segments(nodes, closed):
    """All segments of a path as node tuples: 4-node cubics or 2-node
    lines (quadratic/1-offcurve segments are skipped). Same snapshot
    semantics as `_path_cubic_segments`."""
    oncurve = [i for i, nd in enumerate(nodes) if str(nd.type) != "offcurve"]
    if not oncurve:
        return []
    pairs = list(zip(oncurve, oncurve[1:]))
    if closed and len(oncurve) > 1:
        pairs.append((oncurve[-1], oncurve[0]))
    segments = []
    for i, j in pairs:
        between = nodes[i + 1:j] if j > i else nodes[i + 1:] + nodes[:j]
        if len(between) == 2:
            segments.append((nodes[i], between[0], between[1], nodes[j]))
        elif len(between) == 0:
            segments.append((nodes[i], nodes[j]))
    return segments


def _seg_tangent_at(seg, at_end):
    """Tangent vector of a segment (cubic or line) at its end (or start)."""
    pts = [_node_pos(nd) for nd in seg]
    if len(pts) == 4:
        t0, t1 = cubic_endpoint_tangents(pts)
        return t1 if at_end else t0
    return (pts[-1][0] - pts[0][0], pts[-1][1] - pts[0][1])


def _seg_points(seg):
    return tuple(_node_pos(nd) for nd in seg)


def _record_x_extrema(layer):
    """Positions of on-curve nodes that are true X extrema (vertical
    tangent at a cubic segment endpoint) BEFORE the transform."""
    recorded = []
    for path in layer.paths:
        nodes = list(path.nodes)
        for seg in _path_cubic_segments(nodes, bool(path.closed)):
            t0, t1 = cubic_endpoint_tangents(_seg_points(seg))
            if is_vertical_tangent(t0, tol=0.0):
                recorded.append(_node_pos(seg[0]))
            if is_vertical_tangent(t1, tol=0.0):
                recorded.append(_node_pos(seg[3]))
    return recorded


def _insert_new_x_extrema(layer):
    """Subdivide cubic segments at post-transform X extrema. Returns
    (inserted_count, inserted_positions) — the positions feed the
    handle-harmonization pass.

    Two strategies per path:
    1. GSPath.insertNodeWithPathTime: (keep-shape insertion), verified by
       its return value — it returns nil on failure WITHOUT raising.
    2. Fallback: rebuild the whole node list with our own de Casteljau
       subdivision (`path.nodes` is a settable property — confirmed in the
       GlyphsCore headers).
    """
    inserted = 0
    positions = []
    for path in list(layer.paths):
        nodes = list(path.nodes)
        closed = bool(path.closed)

        # (end_node_index, t) ops on the raw snapshot. pathTime uses the
        # index of the segment's END on-curve node + t — verified headless
        # against GlyphsCore: insert(3.5) on a 12-node circle subdivides
        # the segment ENDING at node 3 (nodes 0→3).
        ops = []
        for seg in _path_cubic_segments(nodes, closed):
            end_idx = nodes.index(seg[3])
            cubic_pts = _seg_points(seg)
            for t in cubic_x_extrema_ts(cubic_pts):
                ops.append((end_idx, float(t)))
                positions.append(cubic_point(cubic_pts, t))
        if not ops:
            continue

        ok = True
        for end_idx, t in sorted(ops, key=lambda op: op[0] + op[1], reverse=True):
            try:
                new_node = path.insertNodeWithPathTime_(end_idx + t)
                if new_node is None:
                    ok = False
                    break
            except Exception:
                ok = False
                traceback.print_exc()
                break
        if ok:
            inserted += len(ops)
            continue

        # Fallback: rebuild the node list manually. Rotate closed paths so
        # the list starts on-curve (rotation preserves the shape and makes
        # the rebuild pass linear), then recompute ops against the rotated
        # indices.
        try:
            if closed:
                oncurve = [i for i, nd in enumerate(nodes) if str(nd.type) != "offcurve"]
                if not oncurve:
                    continue
                k = oncurve[0]
                if k:
                    nodes = nodes[k:] + nodes[:k]
            rot_ops = []
            for seg in _path_cubic_segments(nodes, closed):
                start_idx = nodes.index(seg[0])
                for t in cubic_x_extrema_ts(_seg_points(seg)):
                    rot_ops.append((start_idx, float(t)))
            added = _rebuild_path_with_extrema(path, nodes, closed, rot_ops)
            inserted += added
        except Exception:
            Glyphs.showMacroWindow()
            print("Slant Glyphs: extrema insertion failed for a path; left unchanged.")
            print(traceback.format_exc())
    return inserted, positions


def _make_node(pt, node_type):
    from GlyphsApp import GSNode
    try:
        n = GSNode()
    except Exception:
        n = GSNode.alloc().init()
    try:
        n.position = (float(pt[0]), float(pt[1]))
    except Exception:
        from Foundation import NSMakePoint
        n.position = NSMakePoint(float(pt[0]), float(pt[1]))
    n.type = node_type  # "curve" / "offcurve" (wrapper coerces)
    return n


def _rebuild_path_with_extrema(path, nodes, closed, ops) -> int:
    """Rebuild `path.nodes` with the segments named in `ops` subdivided by
    our own solver. `nodes` must be the (rotated, for closed paths)
    pre-insertion snapshot. Returns nodes added."""
    ops_by_start = {}
    for start_idx, t in ops:
        ops_by_start.setdefault(start_idx, []).append(t)

    n = len(nodes)
    oncurve = [i for i, nd in enumerate(nodes) if str(nd.type) != "offcurve"]
    if not oncurve:
        raise ValueError("path has no on-curve nodes")
    pairs = list(zip(oncurve, oncurve[1:]))
    if closed:
        pairs.append((oncurve[-1], n))  # j == n → end node is nodes[0]

    new_nodes = []
    added = 0
    for i, j in pairs:
        new_nodes.append(nodes[i])
        between = nodes[i + 1:j]
        end_node = nodes[j] if j < n else nodes[0]
        ts = ops_by_start.get(i)
        if ts and len(between) == 2:
            cubic = (
                _node_pos(nodes[i]), _node_pos(between[0]),
                _node_pos(between[1]), _node_pos(end_node),
            )
            pieces = subdivide_cubic_multi(cubic, ts)
            for piece in pieces[:-1]:
                new_nodes.append(_make_node(piece[1], "offcurve"))
                new_nodes.append(_make_node(piece[2], "offcurve"))
                new_nodes.append(_make_node(piece[3], "curve"))
                added += 1
            new_nodes.append(_make_node(pieces[-1][1], "offcurve"))
            new_nodes.append(_make_node(pieces[-1][2], "offcurve"))
        else:
            new_nodes.extend(between)
    if not closed:
        new_nodes.extend(nodes[oncurve[-1]:])

    path.nodes = new_nodes
    return added


def _transform_point(matrix, x, y):
    return (
        matrix[0] * x + matrix[2] * y + matrix[4],
        matrix[1] * x + matrix[3] * y + matrix[5],
    )


def _new_extrema_positions(layer, matrix):
    """Font-unit positions where new X-extremum nodes will be inserted on
    `layer` after transforming by `matrix` — used by the preview pane to
    draw markers. Pure computation; the layer is not touched."""
    positions = []
    for path in layer.paths:
        nodes = list(path.nodes)
        for seg in _path_cubic_segments(nodes, bool(path.closed)):
            cubic = tuple(_transform_point(matrix, *_node_pos(nd)) for nd in seg)
            for t in cubic_x_extrema_ts(cubic):
                positions.append(cubic_point(cubic, t))
    return positions


def _remove_stale_x_extrema(layer, recorded_pts, matrix, gate=_EXTREMA_GATE):
    """Delete pre-slant X-extremum nodes that are no longer extremal.

    Policy, established by headless measurement:
    - cubic+cubic: merge with OUR least-squares fit (0.19-unit deviation
      on a test circle, clean handles) — never Glyphs' keep-shape refit,
      which stretches handles.
    - curve+line join (arch-to-stem): KEEP the node. Covering a curve
      plus a straight stem with one cubic is inherently lossy; Glyphs
      only pulls it off via a degenerate handle as long as the stem —
      the "drawn out" handles this tool exists to avoid. A smooth
      post-slant join node is harmless.
    - line+line corner: plain removal (exact).

    Returns (removed, gated_kept, line_join_kept).
    """
    removed = gated = line_kept = 0
    if not recorded_pts:
        return removed, gated, line_kept
    expected = [_transform_point(matrix, x, y) for x, y in recorded_pts]
    for path in list(layer.paths):
        # Rescan after every mutation: a removal changes the node list, so
        # segment snapshots go stale (and two recorded extrema can share a
        # segment). Bounded: every iteration either removes a node or
        # marks one processed.
        processed = []
        while True:
            nodes = list(path.nodes)
            segments = _path_segments(nodes, bool(path.closed))
            nd = None
            for cand in nodes:
                if str(cand.type) == "offcurve":
                    continue
                cx, cy = _node_pos(cand)
                if any(abs(cx - ex) <= _POS_TOL and abs(cy - ey) <= _POS_TOL
                       for ex, ey in expected) and not any(
                       abs(cx - px) <= _POS_TOL and abs(cy - py) <= _POS_TOL
                       for px, py in processed):
                    nd = cand
                    break
            if nd is None:
                break
            processed.append(_node_pos(nd))
            prev_seg = next((s for s in segments if s[-1] is nd), None)
            next_seg = next((s for s in segments if s[0] is nd), None)
            # Still extremal if either adjacent segment keeps a (near-)
            # vertical tangent at this node — leave it alone.
            still_extremal = False
            if prev_seg is not None:
                still_extremal |= is_vertical_tangent(_seg_tangent_at(prev_seg, True))
            if next_seg is not None:
                still_extremal |= is_vertical_tangent(_seg_tangent_at(next_seg, False))
            if still_extremal:
                continue
            if prev_seg is not None and next_seg is not None:
                if len(prev_seg) < 4 or len(next_seg) < 4:
                    if len(prev_seg) == 2 and len(next_seg) == 2:
                        try:
                            path.removeNode_(nd)
                            removed += 1
                        except Exception:
                            traceback.print_exc()
                    else:
                        line_kept += 1
                    continue
                fit = fit_merged_segment(_seg_points(prev_seg), _seg_points(next_seg))
                if fit is None or fit[2] > gate:
                    gated += 1
                    continue
                h1, h2, _dev = fit
                try:
                    _set_node_pos(prev_seg[1], h1)   # prev's entry handle
                    _set_node_pos(next_seg[2], h2)   # next's exit handle
                    path.removeNodes_([nd, prev_seg[2], next_seg[1]])
                    removed += 1
                except Exception:
                    traceback.print_exc()
                    gated += 1
                continue
            # Node adjacent to a quadratic segment (skipped by
            # _path_segments): leave it rather than guessing.
            gated += 1
    return removed, gated, line_kept


def _harmonize_new_extrema(layer, inserted_positions, gate=4.0):
    """Balance the handle pairs adjacent to each newly inserted extremum
    node (snap vertical, equalize length within the deviation budget).
    Returns the number of nodes harmonized."""
    balanced = 0
    if not inserted_positions:
        return balanced
    for path in list(layer.paths):
        nodes = list(path.nodes)
        segments = _path_cubic_segments(nodes, bool(path.closed))
        targets = [
            nd for nd in nodes
            if str(nd.type) != "offcurve"
            and any(
                abs(_node_pos(nd)[0] - ex) <= 0.5
                and abs(_node_pos(nd)[1] - ey) <= 0.5
                for ex, ey in inserted_positions
            )
        ]
        for nd in targets:
            prev_seg = next((s for s in segments if s[3] is nd), None)
            next_seg = next((s for s in segments if s[0] is nd), None)
            if prev_seg is None or next_seg is None:
                continue
            result = balance_extremum_handles(
                _seg_points(prev_seg), _seg_points(next_seg), gate=gate,
            )
            if result is None:
                continue
            new_prev, new_next = result
            try:
                _set_node_pos(prev_seg[2], new_prev[2])
                _set_node_pos(next_seg[1], new_next[1])
                balanced += 1
            except Exception:
                traceback.print_exc()
    return balanced


# Preview pane geometry (points). The window is non-resizable, so these are
# constants rather than layout constraints.
_PREVIEW_W = 310
_PREVIEW_H = 330


class SlantPanel:
    REGISTRY_KEY = "slant_glyphs"

    @classmethod
    def toggle(cls) -> None:
        reg = _panel_registry()
        existing = reg.get(cls.REGISTRY_KEY)
        if existing is not None:
            try:
                existing.close()
            except Exception:
                traceback.print_exc()
            reg[cls.REGISTRY_KEY] = None
            return
        if Glyphs.font is None:
            Glyphs.showMacroWindow()
            print("Slant Glyphs: open a font first.")
            return
        reg[cls.REGISTRY_KEY] = cls()

    def __init__(self) -> None:
        self.font = Glyphs.font
        # Guard against redraw storms: while a Preview/Apply/Revert batch
        # runs, our own UPDATEINTERFACE callback must not re-render the
        # preview pane for every single node mutation (that re-entrancy
        # can beachball the app).
        self._busy = False
        # Preview state. `_snapshots` holds deep layer copies taken before
        # the first preview, keyed by (glyph_name, master_id); while it's
        # non-empty a preview is active and Revert/close restore from it.
        # `_backgrounds_filled` tracks the backgrounds WE filled so Apply /
        # Revert can clear them without touching user data.
        self._snapshots: dict = {}
        self._backgrounds_filled: list = []
        # Reference-picker state: parallel (kind, ident) specs for the
        # popup, mirroring the audit panel's grammar. `_user_picked_index`
        # records a manual pick so it survives menu rebuilds.
        self._option_specs: list = []
        self._user_picked_index = None
        # Cache of UPM-scaled reference outline paths for the preview pane,
        # keyed by (ref_spec, ref_glyph_name). Building an NSBezierPath via
        # CocoaPen on every UPDATEINTERFACE tick would be wasteful.
        self._ref_path_cache: dict = {}

        self.w = vanilla.FloatingWindow(
            (620, 390),
            "Slant Glyphs",
            autosaveName="GlyphAuditSlantPanel",
        )

        # ----- transform fields -----
        self.w.slantLabel = vanilla.TextBox((10, 14, 90, 20), "Slant (°)", alignment="right")
        self.w.slant = vanilla.EditText((110, 12, 60, 22), "10", callback=self._field_cb)
        self.w.widthLabel = vanilla.TextBox((10, 44, 90, 20), "Width (%)", alignment="right")
        self.w.widthPct = vanilla.EditText((110, 42, 60, 22), "100", callback=self._field_cb)
        self.w.heightLabel = vanilla.TextBox((10, 74, 90, 20), "Height (%)", alignment="right")
        self.w.heightPct = vanilla.EditText((110, 72, 60, 22), "100", callback=self._field_cb)
        self.w.originLabel = vanilla.TextBox((10, 104, 90, 20), "Origin", alignment="right")
        self.w.origin = vanilla.PopUpButton(
            (110, 102, 140, 22), list(PIVOT_CHOICES), callback=self._field_cb,
        )
        self.w.origin.set(PIVOT_CHOICES.index(PIVOT_XHEIGHT))

        # ----- reference picker -----
        self.w.refLabel = vanilla.TextBox((10, 134, 90, 20), "Reference", alignment="right")
        self.w.refMenu = vanilla.PopUpButton(
            (110, 132, -320, 22), [], callback=self._ref_picked_cb,
        )
        self.w.editConfigBtn = vanilla.Button(
            (110, 160, 130, 22), "Edit config…",
            callback=lambda sender: self._edit_config_cb(),
        )

        # ----- options -----
        self.w.allMasters = vanilla.CheckBox(
            (110, 192, -310, 20), "Apply to all masters", value=True,
        )
        self.w.decompose = vanilla.CheckBox(
            (110, 212, -320, 20), "Decompose components", value=True,
        )
        self.w.fixExtrema = vanilla.CheckBox(
            (110, 232, -310, 20), "Fix extrema (insert/remove)", value=True,
        )
        self.w.matchWidths = vanilla.CheckBox(
            (110, 252, -310, 20), "Match reference widths", value=True,
        )

        # ----- actions -----
        self.w.previewBtn = vanilla.Button((10, 282, 85, 24), "Preview", callback=self._preview_cb)
        self.w.revertBtn = vanilla.Button((105, 282, 85, 24), "Revert", callback=self._revert_cb)
        self.w.applyBtn = vanilla.Button((200, 282, 90, 24), "Apply", callback=self._apply_cb)

        self.w.summary = vanilla.TextBox((10, 316, 280, -10), "", sizeStyle="small")

        # ----- preview pane -----
        self.w.preview = vanilla.ImageView(
            (300, 10, _PREVIEW_W, _PREVIEW_H),
            horizontalAlignment="left", verticalAlignment="top", scale="none",
        )
        self.w.legend = vanilla.TextBox(
            (300, 346, 310, 34),
            "Preview: blue = slanted · red = reference · hairlines = advances · dots = new extrema",
            sizeStyle="small",
        )

        self.w.bind("close", self._on_close)
        self._rebuild_ref_menu(select_default=True)
        self._subscribe_live()
        self._set_summary(
            "Select glyphs, set values, hit Preview. Widths match the "
            "resolved reference on Apply."
        )
        self.w.open()
        self._redraw_preview()

    # ----- lifecycle ------------------------------------------------------

    def close(self) -> None:
        try:
            self._unsubscribe_live()
            if self._snapshots:
                self._revert()
            self.w.close()
        finally:
            _panel_registry()[self.REGISTRY_KEY] = None

    def _on_close(self, sender) -> None:
        self._unsubscribe_live()
        if self._snapshots:
            try:
                self._revert()
            except Exception:
                traceback.print_exc()
        _panel_registry()[self.REGISTRY_KEY] = None

    # ----- live preview refresh --------------------------------------------

    def _subscribe_live(self) -> None:
        Glyphs.addCallback(self._on_update, UPDATEINTERFACE)

    def _unsubscribe_live(self) -> None:
        try:
            Glyphs.removeCallback(self._on_update)
        except Exception:
            pass

    def _on_update(self, info=None) -> None:
        if self._busy:
            return
        try:
            self._redraw_preview()
        except Exception:
            traceback.print_exc()

    def _field_cb(self, sender) -> None:
        if self._busy:
            return
        self._redraw_preview()

    def _run_batched(self, fn) -> None:
        """Run `fn` with interface updates suppressed: hundreds of node
        mutations otherwise each fire UPDATEINTERFACE (and our own redraw)
        — the redraw storm that hangs the app."""
        self._busy = True
        try:
            self.font.disableUpdateInterface()
        except Exception:
            pass
        try:
            fn()
        finally:
            self._busy = False
            try:
                self.font.enableUpdateInterface()
            except Exception:
                pass

    # ----- field parsing ---------------------------------------------------

    def _get_float(self, edit, default: float) -> float:
        text = edit.get().strip()
        if not text:
            return default
        return float(text)

    def _params(self):
        angle = self._get_float(self.w.slant, 0.0)
        width_pct = self._get_float(self.w.widthPct, 100.0)
        height_pct = self._get_float(self.w.heightPct, 100.0)
        origin_choice = PIVOT_CHOICES[self.w.origin.get()]
        return angle, width_pct, height_pct, origin_choice

    # ----- selection / scope -------------------------------------------------

    def _selected_glyph_names(self) -> list:
        """Selected glyphs (Edit View selection, else Font View selection),
        deduped, order preserved."""
        names: list = []
        seen = set()
        for layer in (self.font.selectedLayers or []):
            glyph = layer.parent
            if glyph is None or glyph.name in seen:
                continue
            seen.add(glyph.name)
            names.append(glyph.name)
        return names

    def _current_master(self):
        master = self.font.selectedFontMaster
        if master is None and self.font.masters:
            master = self.font.masters[0]
        return master

    def _masters_in_scope(self) -> list:
        if self.w.allMasters.get():
            return list(self.font.masters)
        current = self.font.selectedFontMaster
        return [current] if current is not None else []

    # ----- reference picker -------------------------------------------------
    #
    # Grammar: "auto" (per-master config pin — the default, shared with the
    # Glyph Audit panel), "config" (a specific [instances.*] entry), "file"
    # (a picked/recent file — the recents list lives in the shared
    # audit-panel-state.json, so picks from the other panel appear here),
    # "file_picker" (menu action, not a real reference).

    def _rebuild_ref_menu(self, *, select_default: bool) -> None:
        items = ["Auto (per-master config)"]
        specs = [("auto", None)]
        for master_lc, ref_path in load_audit_references().items():
            short = Path(ref_path).name if "/" in ref_path else ref_path
            items.append(f"{master_lc} → {short}")
            specs.append(("config", master_lc))
        for path in load_recent_files():
            items.append(f"Recent · {Path(path).name}")
            specs.append(("file", path))
        items.append("Choose file…")
        specs.append(("file_picker", None))

        self._option_specs = specs
        self.w.refMenu.setItems(items)
        if select_default:
            self.w.refMenu.set(0)
            self._user_picked_index = None
        elif self._user_picked_index is not None and self._user_picked_index < len(specs):
            self.w.refMenu.set(self._user_picked_index)
        else:
            self.w.refMenu.set(0)

    def _ref_picked_cb(self, sender) -> None:
        idx = sender.get()
        if idx < 0 or idx >= len(self._option_specs):
            return
        kind, ident = self._option_specs[idx]
        if kind == "file_picker":
            # Restore the previous selection while the sheet is up.
            if self._user_picked_index is not None and self._user_picked_index < len(self._option_specs):
                self.w.refMenu.set(self._user_picked_index)
            else:
                self.w.refMenu.set(0)
            try:
                parent = self.w.getNSWindow()
            except Exception:
                parent = None
            pick_font_file_async(parent, on_picked=self._apply_picked_file)
            return
        self._user_picked_index = idx
        self._redraw_preview()

    def _apply_picked_file(self, path: str) -> None:
        push_recent_file(path)
        self._rebuild_ref_menu(select_default=False)
        for i, (k, ident) in enumerate(self._option_specs):
            if k == "file" and ident == path:
                self.w.refMenu.set(i)
                self._user_picked_index = i
                break
        self._redraw_preview()

    def _edit_config_cb(self) -> None:
        open_config_in_editor()
        self._rebuild_ref_menu(select_default=True)
        self._redraw_preview()

    def _ref_spec_for_master(self, master):
        """Resolved reference spec for a master, honouring the popup.

        Auto → the master's own config pin; an explicit config/file pick is
        forced for every master in scope. None when nothing resolves.
        """
        idx = self.w.refMenu.get()
        kind, ident = ("auto", None)
        if 0 <= idx < len(self._option_specs):
            kind, ident = self._option_specs[idx]
        refs = load_audit_references()
        if kind == "config":
            return refs.get(ident)
        if kind == "file":
            return ident
        return refs.get(master.name.lower())

    # ----- snapshot / background --------------------------------------------

    def _take_snapshots(self, names, masters) -> None:
        for master in masters:
            for name in names:
                glyph = self.font.glyphs[name]
                if glyph is None:
                    continue
                layer = glyph.layers[master.id]
                if layer is None:
                    continue
                key = (name, master.id)
                self._snapshots[key] = layer.copy()
                # Visual reference: originals into the background — but only
                # when it's empty. Never clobber an existing background (the
                # source script's unconditional erase was its biggest
                # footgun).
                try:
                    bg = layer.background
                    if bg is not None and _layer_is_empty(bg):
                        _copy_shapes(layer, bg)
                        self._backgrounds_filled.append(key)
                except Exception:
                    traceback.print_exc()

    def _restore_snapshots(self) -> None:
        for (name, master_id), snap in self._snapshots.items():
            glyph = self.font.glyphs[name]
            if glyph is None:
                continue
            glyph.layers[master_id] = snap.copy()

    def _clear_filled_backgrounds(self) -> None:
        for name, master_id in self._backgrounds_filled:
            glyph = self.font.glyphs[name]
            if glyph is None:
                continue
            layer = glyph.layers[master_id]
            if layer is None:
                continue
            try:
                _clear_shapes(layer.background)
            except Exception:
                traceback.print_exc()
        self._backgrounds_filled = []

    # ----- transform ----------------------------------------------------------

    def _slant_scope(self, names, masters):
        """Apply the current transform to every (glyph, master) in scope.
        Returns (layers_transformed, extrema_stats) where extrema_stats is
        a dict with inserted / removed / gated counts."""
        angle, width_pct, height_pct, origin_choice = self._params()
        decompose = bool(self.w.decompose.get())
        fix_extrema = bool(self.w.fixExtrema.get()) and abs(angle) > 1e-9
        stats = {"inserted": 0, "removed": 0, "gated": 0, "balanced": 0, "line_kept": 0}
        count = 0
        for master in masters:
            x_height = getattr(master, "xHeight", 0.0) or 0.0
            cap_height = getattr(master, "capHeight", 0.0) or 0.0
            origin_y = pivot_y(origin_choice, x_height, cap_height)
            matrix = shear_transform(angle, width_pct, height_pct, origin_y)
            for name in names:
                glyph = self.font.glyphs[name]
                if glyph is None:
                    continue
                layer = glyph.layers[master.id]
                if layer is None:
                    continue
                if decompose:
                    _decompose_components(layer)
                recorded = _record_x_extrema(layer) if fix_extrema else []
                layer.applyTransform(matrix)
                if fix_extrema:
                    ins, ins_positions = _insert_new_x_extrema(layer)
                    stats["inserted"] += ins
                    removed, gated, line_kept = _remove_stale_x_extrema(layer, recorded, matrix)
                    stats["removed"] += removed
                    stats["gated"] += gated
                    stats["line_kept"] += line_kept
                    stats["balanced"] += _harmonize_new_extrema(layer, ins_positions)
                count += 1
        return count, stats

    def _extrema_summary(self, stats) -> str:
        if not self.w.fixExtrema.get():
            return ""
        msg = f" Extrema: +{stats['inserted']} inserted, −{stats['removed']} removed"
        if stats.get("balanced"):
            msg += f", {stats['balanced']} balanced"
        if stats["gated"]:
            msg += f", {stats['gated']} kept (shape gate)"
        if stats.get("line_kept"):
            msg += f", {stats['line_kept']} kept (line joins)"
        return msg + "."

    def _compatibility_warnings(self, names, masters) -> list:
        """Glyphs whose total node count now differs across masters —
        interpolation will break on these. Detection only; fixing the
        structure is a manual step."""
        if len(masters) < 2:
            return []
        bad = []
        for name in names:
            glyph = self.font.glyphs[name]
            if glyph is None:
                continue
            counts = set()
            for master in masters:
                layer = glyph.layers[master.id]
                if layer is not None:
                    counts.add(sum(len(list(p.nodes)) for p in layer.paths))
            if len(counts) > 1:
                bad.append(name)
        return bad

    # ----- width matching -------------------------------------------------------

    def _match_widths(self, names, masters):
        """Set each layer's advance from the resolved per-master reference.
        Returns (matched_count, [(master_name, glyph_name), ...] misses)."""
        matched = 0
        misses: list = []
        for master in masters:
            spec = self._ref_spec_for_master(master)
            if not spec:
                misses.extend((master.name, n) for n in names)
                continue
            try:
                ref_view = load_reference_cached(spec)
            except Exception:
                Glyphs.showMacroWindow()
                print(f"Slant Glyphs: reference load failed for master {master.name}")
                print(f"  spec: {spec!r}")
                traceback.print_exc()
                misses.extend((master.name, n) for n in names)
                continue
            for name in names:
                glyph = self.font.glyphs[name]
                if glyph is None:
                    continue
                layer = glyph.layers[master.id]
                if layer is None:
                    continue
                adv = ref_advance(ref_view, name)
                if adv is None:
                    misses.append((master.name, name))
                    continue
                layer.width = scale_advance(adv, ref_view.upm, self.font.upm)
                matched += 1
        return matched, misses

    # ----- preview pane ---------------------------------------------------------

    def _ref_glyph_name(self, name, ref_view, glyphset):
        """Map our glyph name onto a glyph in the reference font: direct
        hit, GSUB variant of the base (a.smcp → the ref's smallcap), or via
        unicode cmap. None when nothing corresponds."""
        if name in glyphset:
            return name
        parsed = parse_variant_suffix(name)
        if parsed:
            base, feature = parsed
            base_glyph = self.font.glyphs[base]
            if base_glyph is not None:
                for u in (base_glyph.unicodes or []):
                    try:
                        cp = int(u, 16) if isinstance(u, str) else int(u)
                    except (TypeError, ValueError):
                        continue
                    variant = ref_view.gsub_variants.get((cp, feature))
                    if variant and variant in glyphset:
                        return variant
                    break
        glyph = self.font.glyphs[name]
        if glyph is not None:
            for u in (glyph.unicodes or []):
                try:
                    cp = int(u, 16) if isinstance(u, str) else int(u)
                except (TypeError, ValueError):
                    continue
                mapped = ref_view.cmap.get(cp)
                if mapped and mapped in glyphset:
                    return mapped
                break
        return None

    def _ref_path(self, ref_view, ref_name, glyphset):
        """Reference glyph outline as an NSBezierPath, scaled into our UPM.
        Cached per (source, glyph) so UPDATEINTERFACE ticks stay cheap."""
        key = (ref_view.source, ref_name)
        cached = self._ref_path_cache.get(key)
        if cached is not None:
            return cached
        try:
            from fontTools.pens.cocoaPen import CocoaPen
        except ImportError:
            return None
        try:
            pen = CocoaPen(glyphset)
            glyphset[ref_name].draw(pen)
        except Exception:
            return None
        path = pen.path
        if path is None:
            return None
        if ref_view.upm != self.font.upm:
            from AppKit import NSAffineTransform
            r = self.font.upm / ref_view.upm
            t = NSAffineTransform.transform()
            t.setTransformStruct_((r, 0.0, 0.0, r, 0.0, 0.0))
            path = path.copy()
            path.transformUsingAffineTransform_(t)
        self._ref_path_cache[key] = path
        return path

    def _redraw_preview(self) -> None:
        try:
            image = self._render_preview()
            if image is not None:
                self.w.preview.setImage(imageObject=image)
        except Exception:
            traceback.print_exc()

    def _render_preview(self):
        """Offscreen-render slanted-current-glyph (blue) over the reference
        glyph (red) at a shared baseline, plus advance hairlines. Purely
        in-memory — the document is never touched here."""
        from AppKit import (
            NSAffineTransform, NSBezierPath, NSBitmapImageRep, NSColor,
            NSDeviceRGBColorSpace, NSGraphicsContext, NSImage, NSMakeRect,
            NSMakeSize,
        )

        scale2 = 2  # render at 2× so the pane stays crisp on Retina
        rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
            None, _PREVIEW_W * scale2, _PREVIEW_H * scale2, 8, 4,
            True, False, NSDeviceRGBColorSpace, 0, 0,
        )
        if rep is None:
            return None
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.setCurrentContext_(
            NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
        )
        try:
            # Work in point coordinates: 2× context scale via concat.
            retina = NSAffineTransform.transform()
            retina.scaleBy_(scale2)
            retina.concat()

            frame = NSMakeRect(0, 0, _PREVIEW_W, _PREVIEW_H)
            NSColor.whiteColor().set()
            NSBezierPath.bezierPathWithRect_(frame).fill()

            master = self._current_master()
            names = self._selected_glyph_names()
            glyph = self.font.glyphs[names[0]] if names else None
            layer = None
            if master is not None and glyph is not None:
                layer = glyph.layers[master.id]

            # --- gather paths (in font units) ---
            t_path = None
            markers = []
            src_path = None
            if layer is not None:
                src_path = getattr(layer, "completeBezierPath", None) or getattr(layer, "bezierPath", None)
            if src_path is not None:
                try:
                    angle, width_pct, height_pct, origin_choice = self._params()
                except ValueError:
                    # Mid-edit field content ("-", "") — skip the target
                    # outline this tick rather than spamming the Macro log.
                    angle = None
                if angle is not None:
                    x_height = getattr(master, "xHeight", 0.0) or 0.0
                    cap_height = getattr(master, "capHeight", 0.0) or 0.0
                    origin_y = pivot_y(origin_choice, x_height, cap_height)
                    matrix = shear_transform(angle, width_pct, height_pct, origin_y)
                    shear = NSAffineTransform.transform()
                    shear.setTransformStruct_(matrix)
                    t_path = src_path.copy()
                    t_path.transformUsingAffineTransform_(shear)
                    if self.w.fixExtrema.get() and abs(angle) > 1e-9:
                        markers = _new_extrema_positions(layer, matrix)

            r_path = None
            r_adv = None
            spec = self._ref_spec_for_master(master) if master is not None else None
            if spec and names:
                try:
                    ref_view = load_reference_cached(spec)
                except Exception:
                    ref_view = None
                if ref_view is not None:
                    ttfont = ttfont_for(ref_view)
                    if ttfont is not None:
                        glyphset = ttfont.getGlyphSet()
                        r_name = self._ref_glyph_name(names[0], ref_view, glyphset)
                        if r_name:
                            r_path = self._ref_path(ref_view, r_name, glyphset)
                            r_adv = ref_view.advances.get(r_name)
                            if r_adv is not None:
                                r_adv = scale_advance(r_adv, ref_view.upm, self.font.upm)

            # --- viewport ---
            pad = 14.0
            upm = float(self.font.upm)
            asc = float(getattr(master, "ascender", 0.0) or 0.8 * upm) if master is not None else 0.8 * upm
            desc = float(getattr(master, "descender", 0.0) or -0.2 * upm) if master is not None else -0.2 * upm
            t_adv = float(layer.width or 0.0) if layer is not None else 0.0
            # Horizontal fit from ink bounds AND advances: italic ink
            # overhangs the advance on the right, and sidebearings can be
            # negative on the left — advances alone clip both.
            min_x = 0.0
            max_x = max(t_adv, r_adv or 0.0, 1.0)
            for pth in (r_path, t_path):
                if pth is None:
                    continue
                b = pth.bounds()
                if b.size.width > 0:
                    min_x = min(min_x, b.origin.x)
                    max_x = max(max_x, b.origin.x + b.size.width)
            content_w = max(max_x - min_x, 1.0)
            s = min(
                (_PREVIEW_H - 2 * pad) / max(asc - desc, 1.0),
                (_PREVIEW_W - 2 * pad) / content_w,
            )
            baseline_y = pad + (-desc) * s
            origin_x = pad + (-min_x) * s

            vt = NSAffineTransform.transform()
            vt.translateXBy_yBy_(origin_x, baseline_y)
            vt.scaleBy_(s)

            # Baseline.
            NSColor.colorWithCalibratedWhite_alpha_(0.75, 1.0).set()
            base = NSBezierPath.bezierPath()
            base.moveToPoint_((0, baseline_y))
            base.lineToPoint_((_PREVIEW_W, baseline_y))
            base.setLineWidth_(0.5)
            base.stroke()

            def _hairline(x, color):
                color.set()
                line = NSBezierPath.bezierPath()
                line.moveToPoint_((x, 0))
                line.lineToPoint_((x, _PREVIEW_H))
                line.setLineWidth_(0.5)
                line.stroke()

            blue = NSColor.systemBlueColor().colorWithAlphaComponent_(0.5)
            red = NSColor.systemRedColor().colorWithAlphaComponent_(0.45)

            # Reference first (underneath), then the slanted target.
            if r_path is not None:
                rp = r_path.copy()
                rp.transformUsingAffineTransform_(vt)
                red.set()
                rp.fill()
            if r_adv:
                _hairline(origin_x + r_adv * s, NSColor.systemRedColor().colorWithAlphaComponent_(0.8))
            if t_path is not None:
                tp = t_path.copy()
                tp.transformUsingAffineTransform_(vt)
                blue.set()
                tp.fill()
            if t_adv:
                _hairline(origin_x + t_adv * s, NSColor.systemBlueColor().colorWithAlphaComponent_(0.8))

            # New-extrema markers (orange dots) — where Preview/Apply will
            # insert nodes.
            if markers:
                NSColor.systemOrangeColor().set()
                d = 5.0
                for mx, my in markers:
                    dot = NSBezierPath.bezierPathWithOvalInRect_(
                        NSMakeRect(origin_x + mx * s - d / 2, baseline_y + my * s - d / 2, d, d)
                    )
                    dot.fill()

            img = NSImage.alloc().initWithSize_(NSMakeSize(_PREVIEW_W, _PREVIEW_H))
            img.addRepresentation_(rep)
            return img
        finally:
            NSGraphicsContext.restoreGraphicsState()

    # ----- callbacks ------------------------------------------------------------

    def _preview_cb(self, sender) -> None:
        try:
            self._run_batched(self._preview)
        except ValueError as e:
            self._set_summary(f"Bad value: {e}")
        except Exception:
            Glyphs.showMacroWindow()
            print("Slant Glyphs: preview failed.")
            print(traceback.format_exc())

    def _preview(self) -> None:
        names = self._selected_glyph_names()
        if not names:
            self._set_summary("No glyphs selected.")
            return
        masters = self._masters_in_scope()
        if not self._snapshots:
            self._take_snapshots(names, masters)
        else:
            # Tweak: re-apply from the originals so iterations never
            # compound transforms.
            self._restore_snapshots()
        count, stats = self._slant_scope(names, masters)
        angle, width_pct, height_pct, _origin = self._params()
        Glyphs.redraw()
        self._redraw_preview()
        print(
            f"Slant Glyphs preview: {count} layers @ {angle:g}° · extrema "
            f"+{stats['inserted']} −{stats['removed']} (gated {stats['gated']})"
        )
        msg = (
            f"Previewing {len(names)} glyph(s) × {len(masters)} master(s) "
            f"@ {angle:g}°, W {width_pct:g}%, H {height_pct:g}% "
            f"({count} layers)." + self._extrema_summary(stats)
        )
        bad = self._compatibility_warnings(names, masters)
        if bad:
            msg += f" ⚠ node counts differ across masters: {', '.join(bad[:5])}."
        msg += " Apply to keep, Revert to undo."
        self._set_summary(msg)

    def _revert_cb(self, sender) -> None:
        try:
            self._run_batched(self._revert)
        except Exception:
            Glyphs.showMacroWindow()
            print("Slant Glyphs: revert failed.")
            print(traceback.format_exc())

    def _revert(self) -> None:
        if not self._snapshots:
            self._set_summary("Nothing to revert.")
            return
        self._restore_snapshots()
        self._clear_filled_backgrounds()
        self._snapshots = {}
        Glyphs.redraw()
        self._redraw_preview()
        self._set_summary("Reverted to pre-preview outlines.")

    def _apply_cb(self, sender) -> None:
        try:
            self._run_batched(self._apply)
        except ValueError as e:
            self._set_summary(f"Bad value: {e}")
        except Exception:
            Glyphs.showMacroWindow()
            print("Slant Glyphs: apply failed.")
            print(traceback.format_exc())

    def _apply(self) -> None:
        names = self._selected_glyph_names()
        if not names and not self._snapshots:
            self._set_summary("No glyphs selected.")
            return
        masters = self._masters_in_scope()
        stats = None
        if self._snapshots:
            # Preview already transformed the outlines; commit as-is.
            # Names from the snapshot keys cover glyphs that may have been
            # deselected since the preview started.
            names = sorted({name for name, _mid in self._snapshots})
        else:
            _count, stats = self._slant_scope(names, masters)
            print(
                f"Slant Glyphs apply: {_count} layers · extrema "
                f"+{stats['inserted']} −{stats['removed']} (gated {stats['gated']})"
            )

        if self.w.matchWidths.get():
            matched, misses = self._match_widths(names, masters)
            msg = f"Applied. Widths matched on {matched} layer(s)"
            if misses:
                preview = ", ".join(f"{m}/{n}" for m, n in misses[:5])
                more = f" +{len(misses) - 5} more" if len(misses) > 5 else ""
                msg += f"; {len(misses)} unmatched (no ref or glyph missing): {preview}{more}"
            msg += "."
        else:
            msg = f"Applied to {len(names)} glyph(s) × {len(masters)} master(s)."
        if stats is not None:
            msg += self._extrema_summary(stats)
        bad = self._compatibility_warnings(names, masters)
        if bad:
            msg += f" ⚠ node counts differ across masters: {', '.join(bad[:5])}."
        self._set_summary(msg)

        # New baseline: drop the preview state.
        self._clear_filled_backgrounds()
        self._snapshots = {}
        Glyphs.redraw()
        self._redraw_preview()

    def _set_summary(self, text: str) -> None:
        self.w.summary.set(text)


# ---------------------------------------------------------------------------
# Entry point. Glyphs.app executes scripts with __name__ set to the file's
# basename (not "__main__"), so don't gate on that. Any exception surfaces
# in the Macro Window instead of failing silently.
# ---------------------------------------------------------------------------

try:
    SlantPanel.toggle()
except Exception:
    Glyphs.showMacroWindow()
    print("Slant Glyphs: failed to launch.")
    print(traceback.format_exc())
