#MenuTitle: Width Audit
# -*- coding: utf-8 -*-
"""Glyphs.app panel for advance-width auditing against a reference font.

Companion to `Glyph Proof.py` — the two used to share one window; splitting
them lets the audit table be a wide/short window instead of one crowded
tall one. This one:

  - Master picker (which master of the currently-open document to audit).
  - Reference dropdown: `[instances.*]` from ~/.glyph-audit/config.toml
    plus recently-picked files (accepted via the file picker). System
    fonts are intentionally NOT enumerated — picking a system family
    lands on whatever redistribution macOS ships, which produces
    inconsistent OT-feature coverage; pin the exact TTF you care about.
  - Live update via Glyphs UPDATEINTERFACE callback — the table follows
    your edits keystroke-by-keystroke.
  - Active-glyph marker: the row for the glyph under the edit-view
    cursor is flagged with ▶ so you can spot its delta at a glance.
  - Advance-width delta column (Δ). Sidebearing (LSB/RSB) columns are
    temporarily out — the current RSB math (advance − LSB − glyf bbox
    width) diverges from Glyphs's own `layer.RSB` on glyphs with
    control-point extents, so the reported deltas were misleading.
    Bearings are still extracted into FontView; the column just isn't
    rendered until we reconcile the definitions.

Toggle: run the menu item again to close.

Install
-------
    pip install docrepair-tools
    glyph-audit proof panel install
"""

import sys
import traceback
from pathlib import Path

# sys.path bootstrap — inline because Glyphs.app runs a symlinked script
# as a standalone module (no `__package__`), so relative imports like
# `from ._bootstrap import …` blow up with ImportError. Walk up from this
# file until we hit a directory with `GlyphAudit/__init__.py` (the src
# root of the checkout, or the site-packages install root) and prepend
# it to sys.path. No-op when GlyphAudit is already importable.
_HERE_INIT = Path(__file__).resolve()
if "GlyphAudit" not in sys.modules:
    for _depth in range(1, min(7, len(_HERE_INIT.parents))):
        _root = _HERE_INIT.parents[_depth]
        if (_root / "GlyphAudit" / "__init__.py").exists():
            if str(_root) not in sys.path:
                sys.path.insert(0, str(_root))
            break

# Purge cached GlyphAudit modules so re-running the menu item picks up
# code changes without restarting Glyphs. Glyphs's embedded Python keeps
# `sys.modules` across "Reload Scripts" — the script file re-runs but
# `import GlyphAudit.…` returns the stale cached module, so edits to
# common.py / matching.py silently never load (symptom: ImportError for
# a name that clearly exists on disk). Cheap: these modules re-import in
# a few ms. Already-open panel instances keep working — they hold bound
# references to their original classes.
for _mod in [m for m in sys.modules if m == "GlyphAudit" or m.startswith("GlyphAudit.")]:
    del sys.modules[_mod]

import vanilla
from typing import Optional
from GlyphsApp import Glyphs, UPDATEINTERFACE, DRAWBACKGROUND

from GlyphAudit.comparator import TieredComparator
from GlyphAudit.model import GLYPHS_COLOR_NAMES
from GlyphAudit.proof.panel.audit_common import (
    filter_for,
    fontview_from_master,
    load_audit_references,
    load_recent_files,
    load_reference_cached,
    open_config_in_editor,
    pick_font_file_async,
    pin_reference_for_master,
    push_recent_file,
    ttfont_for,
)


# The open-panel registry lives on `builtins`, NOT as a class attribute —
# the module-purge above means each menu-item run gets a fresh class whose
# `_instance` would always be None, so toggle-to-close would break and
# every run would stack a new window. `builtins` survives purges.
import builtins as _builtins


def _panel_registry() -> dict:
    reg = getattr(_builtins, "_glyphaudit_panel_registry", None)
    if reg is None:
        reg = {}
        _builtins._glyphaudit_panel_registry = reg
    return reg


class WidthAuditPanel:
    REGISTRY_KEY = "width_audit"

    @classmethod
    def toggle(cls) -> None:
        reg = _panel_registry()
        existing = reg.get(cls.REGISTRY_KEY)
        if existing is not None:
            # Instance may be from a pre-purge module version — its bound
            # methods still work; just close and clear.
            try:
                existing.close()
            except Exception:
                traceback.print_exc()
            reg[cls.REGISTRY_KEY] = None
            return
        if Glyphs.font is None:
            Glyphs.showMacroWindow()
            print("Width Audit: open a font first.")
            return
        reg[cls.REGISTRY_KEY] = cls()

    def __init__(self) -> None:
        self.font = Glyphs.font
        self._live_subscribed = False
        # Edit-view overlay state. `_overlay_ttfont` is populated by
        # `_refresh` when a reference is loaded; the DRAWBACKGROUND
        # callback reads from it. When None, the callback is a no-op.
        self._overlay_subscribed = False
        self._overlay_ttfont = None
        self._overlay_ref_view = None
        self._instances_map = {}
        # Sentinel -1 so the first _refresh always snaps the dropdown to
        # the edit view's active master on panel open.
        self._last_active_master_idx = -1

        # Reference-picker state — same grammar as the retired unified
        # panel: parallel array of (kind, ident) tuples.
        self._option_specs = []
        self._user_picked_index = None

        self._build_ui()
        self._rebuild_reference_menu(select_master_default=True)
        self._subscribe_live()
        self._refresh()
        self.w.open()

    # ----- UI ------------------------------------------------------------

    def _build_ui(self) -> None:
        W, H = 640, 560
        self.w = vanilla.FloatingWindow(
            (W, H), "Width Audit",
            autosaveName="WidthAuditPanel", minSize=(520, 380),
        )
        # Never let macOS window restoration resurrect the panel at launch:
        # with the default restorable=YES, quitting Glyphs with the panel
        # open archives the window and relaunch restores it — bypassing the
        # toggle registry (which starts empty on `builtins`), so the menu
        # would then stack a second window on top of the ghost. The panel
        # must exist only when toggled on from the menu. autosaveName is
        # unaffected: it persists frame position/size only, not visibility.
        _ns = self.w.getNSWindow()
        _ns.setRestorable_(False)
        _ns.disableSnapshotRestoration()
        y = 12

        # Header — active document context.
        family = self.font.familyName if self.font else "(no font)"
        self.w.headerTitle = vanilla.TextBox((10, y, 400, 20), family, sizeStyle="regular")
        y += 22
        self.w.headerSub = vanilla.TextBox(
            (10, y, -10, 14),
            "compares the picked master against the reference — updates live as you edit",
            sizeStyle="mini",
        )
        y += 24

        # Master + filter + live toggle row.
        self.w.masterLabel = vanilla.TextBox((10, y + 4, 50, 18), "Master:", sizeStyle="small")
        master_names = [m.name for m in (self.font.masters if self.font else [])]
        self.w.masterMenu = vanilla.PopUpButton(
            (60, y, 140, 22), master_names, callback=self._master_changed_cb,
        )
        self.w.filterLabel = vanilla.TextBox((210, y + 4, 40, 18), "Filter:", sizeStyle="small")
        # Every COLOR_FILTERS key, ordered: the two workflow filters first
        # (yellow stays index 0 — the long-standing default), then the rest
        # of the palette in Glyphs colour-index order, then no-colour, then
        # the everything-passes escape hatch.
        _palette_order = [
            GLYPHS_COLOR_NAMES[i] for i in sorted(GLYPHS_COLOR_NAMES)
            if GLYPHS_COLOR_NAMES[i] != "yellow"
        ]
        filter_items = ["yellow", "ready"] + _palette_order + ["no-colour", "all"]
        self.w.filterMenu = vanilla.PopUpButton(
            (250, y, 110, 22),
            filter_items,
            callback=self._refresh_cb,
        )
        self.w.liveBox = vanilla.CheckBox(
            (370, y, 55, 22), "Live", value=True, callback=self._live_cb,
        )
        # Overlay toggle — draws the paired reference glyph behind the
        # current edit view. No source modification; the callback reads
        # contours from a cached fontTools TTFont on every draw.
        self.w.overlayBox = vanilla.CheckBox(
            (425, y, 95, 22), "Overlay ref", value=False,
            callback=self._overlay_cb,
        )
        self.w.refreshBtn = vanilla.Button(
            (-90, y, 80, 22), "Refresh", callback=self._refresh_cb,
        )
        y += 28

        # Reference picker row. "Pin to master" writes the selected file
        # into `[instances.<master>]` in ~/.glyph-audit/config.toml, so
        # the mapping persists and master-switching auto-selects it —
        # pin each master once and stop thinking about the dropdown.
        self.w.refLabel = vanilla.TextBox((10, y + 4, 70, 18), "Reference:", sizeStyle="small")
        self.w.refMenu = vanilla.PopUpButton(
            (80, y, -230, 22), [], callback=self._reference_picked_cb,
        )
        self.w.pinBtn = vanilla.Button(
            (-220, y - 2, 110, 22), "Pin to master",
            callback=lambda sender: self._pin_to_master_cb(),
        )
        self.w.editConfigBtn = vanilla.Button(
            (-100, y - 2, 90, 22), "Edit config…",
            callback=lambda sender: self._edit_config_cb(),
        )
        y += 28

        self.w.summary = vanilla.TextBox((10, y, -10, 18), "", sizeStyle="small")
        y += 22

        # LSB / RSB delta columns temporarily removed — the current
        # RSB computation (advance − LSB − glyf bbox width) diverges
        # from Glyphs.app's `layer.RSB` for glyphs with control-point
        # extents, so the reported deltas were misleading. The
        # underlying `left_sidebearings` / `right_sidebearings` fields
        # on FontView stay populated so we can put the columns back
        # once the bearing math is reconciled with Glyphs's definition.
        cols = [
            dict(title="",      key="active", width=16,  editable=False),
            dict(title="Glyph",  key="name",   width=170, editable=False),
            dict(title="Tier",   key="tier",   width=40,  editable=False),
            dict(title="Target", key="target", width=64,  editable=False),
            dict(title="Ref",    key="ref",    width=64,  editable=False),
            dict(title="Δ",      key="delta",  width=60,  editable=False),
            dict(title="Color",  key="color",  width=80,  editable=False),
        ]
        self.w.list = vanilla.List(
            (10, y, -10, -10),
            [],
            columnDescriptions=cols,
            doubleClickCallback=self._open_glyph_cb,
            allowsMultipleSelection=False,
            autohidesScrollers=False,
        )

        self.w.bind("close", self._on_close)

    # ----- lifecycle ------------------------------------------------------

    def close(self) -> None:
        try:
            self._unsubscribe_live()
            self._unsubscribe_overlay()
            self.w.close()
        finally:
            _panel_registry()[self.REGISTRY_KEY] = None

    def _on_close(self, sender) -> None:
        _panel_registry()[self.REGISTRY_KEY] = None
        self._unsubscribe_live()
        self._unsubscribe_overlay()

    # ----- live updates --------------------------------------------------

    def _subscribe_live(self) -> None:
        if self._live_subscribed:
            return
        Glyphs.addCallback(self._on_update, UPDATEINTERFACE)
        self._live_subscribed = True

    def _unsubscribe_live(self) -> None:
        if not self._live_subscribed:
            return
        try:
            Glyphs.removeCallback(self._on_update)
        except Exception:
            pass
        self._live_subscribed = False

    def _live_cb(self, sender) -> None:
        if sender.get():
            self._subscribe_live()
        else:
            self._unsubscribe_live()

    def _on_update(self, info=None) -> None:
        try:
            self._refresh()
        except Exception:
            traceback.print_exc()

    # ----- edit-view overlay ---------------------------------------------

    def _overlay_cb(self, sender) -> None:
        if sender.get():
            self._subscribe_overlay()
        else:
            self._unsubscribe_overlay()

    def _subscribe_overlay(self) -> None:
        if self._overlay_subscribed:
            return
        Glyphs.addCallback(self._draw_reference_overlay, DRAWBACKGROUND)
        self._overlay_subscribed = True
        # Force Glyphs to redraw so the overlay appears immediately.
        try:
            Glyphs.redraw()
        except Exception:
            pass

    def _unsubscribe_overlay(self) -> None:
        if not self._overlay_subscribed:
            return
        try:
            Glyphs.removeCallback(self._draw_reference_overlay)
        except Exception:
            pass
        self._overlay_subscribed = False
        try:
            Glyphs.redraw()
        except Exception:
            pass

    def _draw_reference_overlay(self, layer, info=None) -> None:
        """Glyphs DRAWBACKGROUND callback. Fires once per glyph draw; keep
        it fast — anything that can fail is guarded because a raised
        exception silently unregisters the callback.

        Placement: origin-aligned. The reference's advance origin (x=0)
        sits on the layer's origin, so an LSB difference reads directly
        as a horizontal ink shift. On top of the outline we render:

          - amber band between the two ink-left edges → the LSB delta,
            made visible and self-quantifying (the band IS the delta);
          - blue band between the two advance edges → total width delta;
          - dashed blue line at the reference's advance edge, so each
            glyph's RSB is readable as ink-right → its own advance line.

        Bands honour the audit's 1-unit tolerance — matching metrics draw
        nothing, so a clean glyph stays clean.

        Outline renders AS-IS: no scale, no skew, no italic-angle
        correction. If the reference and source differ in UPM or italic
        angle, that visual offset IS the signal — matches the
        "match-but-not-quite" workflow this overlay exists for.
        """
        if layer is None:
            return
        gs_glyph = getattr(layer, "parent", None)
        if gs_glyph is None:
            return

        # Resolve the reference for THIS LAYER's master, honouring the
        # per-master pins. The edit view's master and the panel dropdown
        # can disagree (edit Bold Italic while the panel audits Italic),
        # and pairing across masters produces phantom advance bands —
        # e.g. ech_yiwn-arm Bold Italic (1835) vs Verdana Italic (1686)
        # painted a "+149" band the audit table (+5, per-master-correct)
        # never showed. Falls back to the panel's selection when no pin
        # exists for the layer's master.
        ttfont = self._overlay_ttfont
        ref_view = self._overlay_ref_view
        try:
            master = getattr(layer, "master", None)
            if master is None:
                master = layer.associatedFontMaster()
            pin = self._instances_map.get(master.name.lower()) if master is not None else None
        except Exception:
            pin = None
        if pin:
            try:
                pinned_view = load_reference_cached(pin)
                pinned_tt = ttfont_for(pinned_view)
                if pinned_tt is not None:
                    ttfont, ref_view = pinned_tt, pinned_view
            except Exception:
                pass  # bad pin path — panel selection remains the fallback
        if ttfont is None:
            return

        ref_name = self._pair_reference_glyph(gs_glyph, ttfont, ref_view)
        if not ref_name:
            return

        try:
            from fontTools.pens.cocoaPen import CocoaPen
        except ImportError:
            return
        try:
            glyphset = ttfont.getGlyphSet()
            if ref_name not in glyphset:
                return
            pen = CocoaPen(glyphset)
            glyphset[ref_name].draw(pen)
        except Exception:
            return
        path = pen.path
        if path is None:
            return

        try:
            from AppKit import NSBezierPath, NSColor, NSMakeRect
            # `Scale` in the info dict is the current zoom factor; dividing
            # our line width by it keeps strokes at ~1px screen thickness
            # at every zoom level.
            scale = 1.0
            if isinstance(info, dict):
                scale = float(info.get("Scale", 1.0) or 1.0)
            hairline = max(1.0 / scale, 0.001)

            # --- metric-difference bands (under the outline) -----------
            # Ink bounds: NSBezierPath.bounds() accounts for curve extrema,
            # matching GSLayer.bounds' ink-box semantics.
            r_b = path.bounds()
            t_b = layer.bounds
            have_ref_ink = r_b.size.width > 0
            have_t_ink = t_b.size.width > 0

            ys = []
            if have_ref_ink:
                ys += [r_b.origin.y, r_b.origin.y + r_b.size.height]
            if have_t_ink:
                ys += [t_b.origin.y, t_b.origin.y + t_b.size.height]
            band_y0 = min(ys) if ys else -500.0
            band_y1 = max(ys) if ys else 1500.0

            # Italic slant for the metric elements. Glyphs draws italic
            # metric boundaries slanted by the master's italicAngle,
            # pivoting at half x-height (where italic sidebearings are
            # measured) — the advance band + marker follow that so they
            # sit parallel to Glyphs's own width line. The amber ink band
            # stays upright: it compares bounding-box edges, which really
            # are vertical regardless of slant.
            import math
            master = getattr(layer, "master", None)
            if master is None:
                try:
                    master = layer.associatedFontMaster()
                except Exception:
                    master = None
            angle = float(getattr(master, "italicAngle", 0.0) or 0.0) if master is not None else 0.0
            slant = math.tan(math.radians(angle)) if angle else 0.0
            pivot = (float(getattr(master, "xHeight", 0.0) or 0.0) / 2.0) if master is not None else 0.0

            def _sx(x, y):
                """x-position of a slanted metric line at height y."""
                return x + slant * (y - pivot)

            def fill_band(x0, x1, r, g, b, slanted):
                # 1-unit tolerance, same as the audit table — matching
                # metrics get no band at all.
                if abs(x1 - x0) <= 1.0:
                    return
                NSColor.colorWithRed_green_blue_alpha_(r, g, b, 0.18).set()
                lo, hi = min(x0, x1), max(x0, x1)
                if slanted and slant:
                    p = NSBezierPath.bezierPath()
                    p.moveToPoint_((_sx(lo, band_y0), band_y0))
                    p.lineToPoint_((_sx(hi, band_y0), band_y0))
                    p.lineToPoint_((_sx(hi, band_y1), band_y1))
                    p.lineToPoint_((_sx(lo, band_y1), band_y1))
                    p.closePath()
                    p.fill()
                else:
                    rect = NSMakeRect(lo, band_y0, hi - lo, band_y1 - band_y0)
                    NSBezierPath.bezierPathWithRect_(rect).fill()

            # LSB delta: origins are aligned, so the strip between the two
            # ink-left edges is exactly the sidebearing difference. Slanted
            # like the metric lines — an upright bar reads as broken inside
            # an italic edit view, even though the bbox edges it anchors to
            # are technically vertical. Band width (the delta) is identical
            # either way.
            if have_ref_ink and have_t_ink:
                fill_band(t_b.origin.x, r_b.origin.x, 0.76, 0.42, 0.18,
                          slanted=True)  # amber

            # Advance delta + reference advance marker — slanted to match
            # the italic metric lines Glyphs itself draws.
            ref_adv = None
            if ref_view is not None:
                ref_adv = ref_view.advances.get(ref_name)
            if ref_adv is not None:
                fill_band(float(layer.width), float(ref_adv), 0.23, 0.42, 0.72,
                          slanted=True)  # blue
                marker = NSBezierPath.bezierPath()
                marker.moveToPoint_((_sx(float(ref_adv), band_y0), band_y0))
                marker.lineToPoint_((_sx(float(ref_adv), band_y1), band_y1))
                marker.setLineWidth_(hairline)
                marker.setLineDash_count_phase_([4.0 / scale, 3.0 / scale], 2, 0.0)
                NSColor.colorWithRed_green_blue_alpha_(0.23, 0.42, 0.72, 0.6).set()
                marker.stroke()

            # Info label under the glyph: which reference glyph got paired
            # and the advance numbers. Makes a wrong reference selection
            # (e.g. the upright Regular pinned while editing the Italic
            # master) immediately self-explanatory instead of a mystery
            # band. Drawn small and grey; scales with zoom.
            try:
                from AppKit import (
                    NSFont,
                    NSFontAttributeName,
                    NSForegroundColorAttributeName,
                    NSString,
                )
                if ref_adv is not None:
                    delta = float(layer.width) - float(ref_adv)
                    label = f"ref {ref_name} · adv {int(ref_adv)} · Δ {delta:+.0f}"
                else:
                    label = f"ref {ref_name} · (no advance)"
                # Quantify the amber band too — a small LSB delta collapses
                # the band into a thin line that would otherwise be a
                # "what is this?" mystery.
                if have_ref_ink and have_t_ink:
                    lsb_delta = t_b.origin.x - r_b.origin.x
                    if abs(lsb_delta) > 1.0:
                        label += f" · inkL Δ {lsb_delta:+.0f}"
                attrs = {
                    NSFontAttributeName: NSFont.systemFontOfSize_(11.0 / scale),
                    NSForegroundColorAttributeName:
                        NSColor.colorWithRed_green_blue_alpha_(0.45, 0.45, 0.45, 0.9),
                }
                NSString.stringWithString_(label).drawAtPoint_withAttributes_(
                    (0.0, band_y0 - 40.0 / scale), attrs,
                )
            except Exception:
                pass

            # --- reference outline --------------------------------------
            # Subdued grey stroke, matching Glyphs's own default background
            # rendering.
            NSColor.colorWithRed_green_blue_alpha_(0.5, 0.5, 0.5, 0.5).set()
            path.setLineWidth_(hairline)
            path.stroke()

            # --- node points ---------------------------------------------
            # Circles at the reference's nodes, echoing how Glyphs renders
            # a real background layer: hollow circles for on-curve points,
            # small filled dots for off-curve handles. Sized in screen
            # pixels (divided by zoom) so they stay readable at any scale.
            try:
                # DecomposingRecordingPen, NOT plain RecordingPen: composite
                # reference glyphs (accented letters, most smcp variants)
                # record `addComponent` ops verbatim under the plain pen,
                # yielding zero node points even though the outline pen
                # (CocoaPen/BasePen) decomposes them fine — glyphs showed
                # an outline but no circles. The decomposing variant
                # resolves components to transformed contours.
                from fontTools.pens.recordingPen import DecomposingRecordingPen
                rec = DecomposingRecordingPen(glyphset)
                glyphset[ref_name].draw(rec)
                on_pts, off_pts = [], []
                for op, args in rec.value:
                    if op in ("moveTo", "lineTo"):
                        on_pts.append(args[0])
                    elif op in ("curveTo", "qCurveTo"):
                        # Last point is on-curve; the rest are handles.
                        # TrueType qCurveTo may end in None (an all-off-curve
                        # closed contour) — skip that sentinel.
                        *offs, last = args
                        off_pts.extend(pt for pt in offs if pt is not None)
                        if last is not None:
                            on_pts.append(last)
                r_on = 3.0 / scale
                r_off = 1.7 / scale
                node_color = NSColor.colorWithRed_green_blue_alpha_(0.5, 0.5, 0.5, 0.6)
                node_color.set()
                for (px, py) in on_pts:
                    oval = NSBezierPath.bezierPathWithOvalInRect_(
                        NSMakeRect(px - r_on, py - r_on, 2 * r_on, 2 * r_on)
                    )
                    oval.setLineWidth_(hairline)
                    oval.stroke()
                for (px, py) in off_pts:
                    NSBezierPath.bezierPathWithOvalInRect_(
                        NSMakeRect(px - r_off, py - r_off, 2 * r_off, 2 * r_off)
                    ).fill()
            except Exception:
                pass
        except Exception:
            return

    def _pair_reference_glyph(self, gs_glyph, ttfont=None, ref_view=None):
        """Codepoint first, then feature-suffix mapping for variants.

        `ttfont` / `ref_view` are the reference resolved for the layer
        being drawn (per-master pin aware); they default to the panel's
        current selection for any older call sites.

        Matches the pairing rule agreed for the (future) fill script:

          - If the target glyph carries a unicode value, look it up
            directly in the reference cmap.
          - If the target is a suffixed variant (`a.smcp`, `I.ss01`),
            parse the suffix, resolve the base's codepoint, then apply
            the reference's GSUB feature to get the corresponding
            variant glyph. Falls back to the base glyph when the
            reference doesn't ship that variant — better a small-cap-
            shaped base than nothing.
          - Returns None when nothing can be resolved.
        """
        if ttfont is None:
            ttfont = self._overlay_ttfont
        if ref_view is None:
            ref_view = self._overlay_ref_view
        if ttfont is None:
            return None

        # Direct codepoint lookup.
        unis = getattr(gs_glyph, "unicodes", None) or []
        if unis:
            try:
                cp = int(unis[0], 16) if isinstance(unis[0], str) else int(unis[0])
            except (TypeError, ValueError):
                cp = None
            if cp is not None:
                name = ttfont.getBestCmap().get(cp)
                if name:
                    return name

        # Suffix-mapped variant: `parse_variant_suffix` recognises the
        # feature-tag suffixes GlyphAudit already understands (smcp,
        # ss01/02, subs/sups, etc.). Reference-side GSUB mapping comes
        # from the loaded FontView's `gsub_variants` dict — same source
        # of truth the width-audit's T2 tier uses.
        try:
            from GlyphAudit.model import parse_variant_suffix
        except ImportError:
            return None
        gname = getattr(gs_glyph, "name", "") or ""
        parsed = parse_variant_suffix(gname)
        if parsed is None:
            return None
        base_name, feature_tag = parsed

        # Codepoint of the base — via the source font's cmap. The base
        # glyph must live in the same source (that's what makes it a
        # "variant of" — the source cmap has it).
        base_cp = None
        base_glyph = self.font.glyphs[base_name] if self.font else None
        if base_glyph is not None:
            for u in (base_glyph.unicodes or []):
                try:
                    base_cp = int(u, 16) if isinstance(u, str) else int(u)
                    break
                except (TypeError, ValueError):
                    continue
        if base_cp is None:
            return None

        # Ask the reference FontView for its variant of the base.
        if ref_view is None:
            return None
        variant_name = ref_view.gsub_variants.get((base_cp, feature_tag))
        if variant_name:
            return variant_name
        # Reference lacks that variant — draw the base as a fallback so
        # the overlay isn't blank; better than nothing while still
        # signalling the coverage gap (base shape vs your custom variant).
        return ref_view.cmap.get(base_cp)

    # ----- reference picker ---------------------------------------------

    def _rebuild_reference_menu(self, *, select_master_default: bool) -> None:
        # System fonts intentionally NOT enumerated — picking a system
        # family lands on whichever redistribution macOS ships (Apple's
        # Verdana ≠ Microsoft's Verdana in GSUB coverage), which produces
        # confusing audit results. User-supplied files are the source of
        # truth: pin what you actually want to compare against via the
        # file picker or `[instances.NAME]` in ~/.glyph-audit/config.toml.
        items = ["Choose file…"]
        specs = [("file_picker", None)]

        for master_lc, ref_path in load_audit_references().items():
            short = Path(ref_path).name if "/" in ref_path else ref_path
            items.append(f"Config · {master_lc} → {short}")
            specs.append(("config", master_lc))

        for path in load_recent_files():
            items.append(f"Recent · {Path(path).name}")
            specs.append(("file", path))

        self._option_specs = specs
        self.w.refMenu.setItems(items)
        if select_master_default:
            self._select_default_for_master()

    def _select_default_for_master(self) -> None:
        if self.font is None:
            return
        master_idx = self.w.masterMenu.get()
        if master_idx < 0 or master_idx >= len(self.font.masters):
            return
        master_lc = self.font.masters[master_idx].name.lower()
        for i, (kind, ident) in enumerate(self._option_specs):
            if kind == "config" and ident == master_lc:
                self.w.refMenu.set(i)
                self._user_picked_index = None
                return
        if self._user_picked_index is not None and self._user_picked_index < len(self._option_specs):
            self.w.refMenu.set(self._user_picked_index)
            return
        for i, (kind, _) in enumerate(self._option_specs):
            if kind == "file_picker":
                self.w.refMenu.set(i)
                return

    def _reference_picked_cb(self, sender) -> None:
        idx = sender.get()
        if idx < 0 or idx >= len(self._option_specs):
            return
        kind, ident = self._option_specs[idx]
        if kind == "file_picker":
            self._restore_previous_selection()
            try:
                parent = self.w.getNSWindow()
            except Exception:
                parent = None
            pick_font_file_async(parent, on_picked=self._apply_picked_file)
            return
        self._user_picked_index = idx
        self._refresh()

    def _restore_previous_selection(self) -> None:
        if self._user_picked_index is not None and self._user_picked_index < len(self._option_specs):
            self.w.refMenu.set(self._user_picked_index)
        else:
            self._select_default_for_master()

    def _apply_picked_file(self, path: str) -> None:
        push_recent_file(path)
        self._rebuild_reference_menu(select_master_default=False)
        for i, (k, ide) in enumerate(self._option_specs):
            if k == "file" and ide == path:
                self.w.refMenu.set(i)
                self._user_picked_index = i
                break
        self._refresh()

    def _edit_config_cb(self) -> None:
        open_config_in_editor()
        self._rebuild_reference_menu(select_master_default=True)
        self._refresh()

    def _pin_to_master_cb(self) -> None:
        """Persist the currently-selected file reference as this master's
        default. After pinning, switching to this master auto-selects the
        pinned file (via `_select_default_for_master`) — pin each master
        once and the dropdown takes care of itself.
        """
        if self.font is None:
            return
        master_idx = self.w.masterMenu.get()
        if master_idx < 0 or master_idx >= len(self.font.masters):
            return
        master_name = self.font.masters[master_idx].name

        idx = self.w.refMenu.get()
        if idx < 0 or idx >= len(self._option_specs):
            return
        kind, ident = self._option_specs[idx]
        if kind == "config":
            self.w.summary.set(f"'{ident}' is already a pinned config entry.")
            return
        if kind != "file":
            self.w.summary.set(
                "Pick a font file first (Choose file… or a Recent entry), then pin."
            )
            return

        try:
            pin_reference_for_master(master_name, ident)
        except Exception as e:
            self.w.summary.set(f"Pin failed: {type(e).__name__}: {e}")
            traceback.print_exc()
            return

        # Rebuild so the new Config entry appears, and let master-default
        # selection land on it immediately.
        self._user_picked_index = None
        self._rebuild_reference_menu(select_master_default=True)
        self._refresh()

    def _master_changed_cb(self, sender) -> None:
        self._select_default_for_master()
        self._refresh()

    def _resolve_reference_spec(self, master_name: str = ""):
        # `master_name` is unused now that system-font auto-pairing is
        # gone. Signature kept so the call site doesn't churn — makes
        # re-adding system fonts later a smaller diff.
        del master_name
        idx = self.w.refMenu.get()
        if idx < 0 or idx >= len(self._option_specs):
            return None, None
        kind, ident = self._option_specs[idx]
        if kind == "config":
            ref = load_audit_references().get(ident)
            return ref, f"config · {ident}"
        if kind == "file":
            return ident, f"file · {Path(ident).name}"
        return None, None

    # ----- refresh -------------------------------------------------------

    def _refresh_cb(self, sender) -> None:
        self._refresh()

    def _active_glyph_name(self) -> Optional[str]:
        """Name of the glyph under the edit-view cursor, else None.

        `currentTab.layers` is the tab's text as layers; `layersCursor`
        indexes the one being edited. None when the focus is the Font
        view (no edit tab) or the cursor is between characters.
        """
        try:
            tab = self.font.currentTab if self.font is not None else None
            if tab is None:
                return None
            layers = tab.layers
            cursor = tab.layersCursor
            if layers and 0 <= cursor < len(layers):
                return getattr(layers[cursor].parent, "name", None)
        except Exception:
            pass
        return None

    def _refresh(self) -> None:
        if self.font is None or Glyphs.font is not self.font:
            self.font = Glyphs.font
        if self.font is None:
            self.w.summary.set("No font open.")
            self.w.list.set([])
            return

        # Auto-follow the actively-edited master. UPDATEINTERFACE fires on
        # master switches in the edit view, so this runs exactly when it
        # needs to. Snap only when the ACTIVE master changed since we last
        # looked — a manual dropdown override therefore sticks until the
        # user genuinely switches masters in Glyphs, instead of being
        # yanked back on every keystroke's refresh.
        try:
            active_idx = int(getattr(self.font, "masterIndex", -1))
        except Exception:
            active_idx = -1
        if 0 <= active_idx < len(self.font.masters) and active_idx != self._last_active_master_idx:
            self._last_active_master_idx = active_idx
            if self.w.masterMenu.get() != active_idx:
                # vanilla's set() doesn't fire the callback — sync the
                # reference default manually, same as _master_changed_cb.
                self.w.masterMenu.set(active_idx)
                self._select_default_for_master()

        master_idx = self.w.masterMenu.get()
        if master_idx >= len(self.font.masters):
            master_idx = 0
            self.w.masterMenu.set(0)
        master = self.font.masters[master_idx]

        ref_spec, ref_label = self._resolve_reference_spec(master.name)
        if not ref_spec:
            self.w.summary.set(
                "No reference — pick a system font, choose a file, or Edit config…"
            )
            self.w.list.set([])
            return

        try:
            ref_view = load_reference_cached(ref_spec)
        except Exception as e:
            self.w.summary.set(
                f"Reference load failed ({ref_label}): {type(e).__name__}: {e}"
            )
            self.w.list.set([])
            Glyphs.showMacroWindow()
            print("Width Audit — reference load failed")
            print(f"  spec: {ref_spec!r}")
            print(f"  label: {ref_label}")
            traceback.print_exc()
            return

        # Keep the overlay's ref state in step with the picker. Cheap
        # (both are cached per-path); harmless when the overlay checkbox
        # is off since `_draw_reference_overlay` early-returns when it
        # fires without a subscribed panel.
        self._overlay_ttfont = ttfont_for(ref_view)
        self._overlay_ref_view = ref_view  # feeds `_pair_reference_glyph` for variant GSUB lookups
        # Per-master pin map, cached here so the draw callback doesn't
        # re-read the TOML on every frame. The overlay prefers the pin
        # matching the *edit view's* master over the panel's dropdown —
        # the edit view and the panel can be on different masters, and
        # comparing a Bold Italic layer against the Italic reference
        # produces a phantom ~150u advance band (the ech_yiwn-arm bug).
        self._instances_map = load_audit_references()

        target_view = fontview_from_master(self.font, master)
        gfilter, filter_label = filter_for(target_view, self.w.filterMenu.getItem())

        comp = TieredComparator(tolerance_units=1.0)
        result = comp.compare(
            target_view, ref_view,
            pair_label=master.name,
            target_filter=gfilter,
            filter_label=filter_label,
        )

        rows = []
        active_name = self._active_glyph_name()
        for r in result.codepoint_rows:
            if r.status != "mismatch":
                continue
            rows.append(self._row(r.target_name, "T1",
                                  r.target_advance, r.reference_advance,
                                  r.delta, target_view, ref_view,
                                  ref_name=r.reference_name,
                                  active=(r.target_name == active_name)))
        for r in result.variant_rows:
            if r.status != "mismatch":
                continue
            rows.append(self._row(f"{r.target_name}  ({r.feature})", "T2",
                                  r.target_advance, r.reference_advance,
                                  r.delta, target_view, ref_view,
                                  real_name=r.target_name,
                                  ref_name=r.reference_name,
                                  active=(r.target_name == active_name)))

        rows.sort(key=lambda r: abs(int(r["delta"] or 0)), reverse=True)

        # Attach the resolved file basename for system-lookup references.
        # System-picked Verdana isn't always the file you'd expect — macOS
        # ships multiple copies in different dirs, sometimes with different
        # OT feature sets (e.g. missing `smcp` in Apple-supplied variants).
        # Exposing the basename makes the mismatch obvious without hunting.
        src_hint = ""
        raw_src = getattr(ref_view, "source", "") or ""
        if "(" in raw_src and raw_src.endswith(")"):
            # `_load_system` writes `"Family Style (/path/to/file.ttf)"`.
            candidate = raw_src.rsplit("(", 1)[-1].rstrip(")")
            candidate = candidate.split("/")[-1] if "/" in candidate else candidate
            if candidate and candidate != raw_src:
                src_hint = f" ← {candidate}"

        counts = result.counts()
        self.w.summary.set(
            f"{len(rows)} mismatch{'es' if len(rows) != 1 else ''}  ·  "
            f"T1 {counts['tier1']['mismatch']}/{counts['tier1']['match'] + counts['tier1']['mismatch']}, "
            f"T2 {counts['tier2']['mismatch']}/{counts['tier2']['match'] + counts['tier2']['mismatch']}  ·  "
            f"filter={filter_label or 'all'}  ·  master={master.name}  ·  ref={ref_label}{src_hint}"
        )
        self.w.list.set(rows)

    def _row(self, display_name, tier, target_adv, ref_adv, delta, view, ref_view,
             *, real_name=None, ref_name=None, active=False):
        # `view` / `ref_view` / `ref_name` accepted for signature stability
        # while LSB/RSB columns are temporarily out — the sidebearings math
        # is unreconciled with Glyphs's own `layer.LSB` / `.RSB` (RSB via
        # glyf bbox drifts on glyphs with control-point extents). Once the
        # per-loader convention is aligned, re-add the columns without a
        # caller signature change.
        gname = real_name or display_name
        color_idx = view.colors.get(gname)

        return dict(
            name=display_name,
            tier=tier,
            target=str(target_adv) if target_adv is not None else "—",
            ref=str(ref_adv) if ref_adv is not None else "—",
            delta=f"{delta:+.0f}" if delta is not None else "",
            color=GLYPHS_COLOR_NAMES.get(color_idx, ""),
            active="▶" if active else "",
            _name=gname,
        )

    def _open_glyph_cb(self, sender) -> None:
        sel = sender.getSelection()
        if not sel or self.font is None:
            return
        item = sender.get()[sel[0]]
        name = item.get("_name") or item["name"].split(" ")[0]
        master_idx = self.w.masterMenu.get()
        try:
            self.font.masterIndex = master_idx
            tab = self.font.newTab("/" + name)
            if tab is not None:
                try:
                    tab.masterIndex = master_idx
                except Exception:
                    pass
        except Exception:
            traceback.print_exc()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

try:
    WidthAuditPanel.toggle()
except Exception:
    Glyphs.showMacroWindow()
    print("Width Audit: failed to launch.")
    print(traceback.format_exc())
