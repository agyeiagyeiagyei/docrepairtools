#MenuTitle: Width Audit Panel
# -*- coding: utf-8 -*-
"""Floating Vanilla panel for Glyphs.app that shows live width mismatches
against a reference font. Stays above the editing UI so you can keep
drawing or typing with the audit list visible.

Toggle: run the menu item again to close.

INSTALL
-------
Symlink this file into Glyphs's user-scripts folder:

    ln -sf "$(pwd)/Width Audit Panel.py" \\
        "$HOME/Library/Application Support/Glyphs 3/Scripts/Width Audit Panel.py"

Then in Glyphs: hold Option + click the Script menu → Reload Scripts
(or just relaunch Glyphs). The panel appears under Script → Width Audit Panel.

CONFIGURE
---------
References come from your usual GlyphAudit config at
~/.glyph-audit/config.toml. Each `[instances.NAME]` entry is matched
against the current font's master names (case-insensitive):

    [instances.Regular]
    ref = "/path/to/Reference-Regular.ttf"

    [instances.Bold]
    ref = "/path/to/Reference-Bold.ttf"

Variable fonts, system fonts, and Glyphs sources all work as `ref`
values — same syntax as the CLI. See docs/configuration.md.

The Glyphs Python env needs the `GlyphAudit` package importable. If
running from a checkout, the script auto-adds the sibling `src/`
directory to sys.path; otherwise install the package once via:

    /Applications/Glyphs\\ 3.app/Contents/Frameworks/Python.framework/Versions/Current/bin/pip3 install glyph-audit
"""

import sys
import traceback
from pathlib import Path

# Make the in-repo GlyphAudit importable without pip-installing into Glyphs's
# Python env. Layout assumption: this file lives at
# <repo>/examples/glyphs/width_audit_panel.py, so the package src is two
# directories up plus "src". Falls back silently if the layout doesn't match,
# in which case we rely on GlyphAudit being pip-installed.
_HERE = Path(__file__).resolve()
for candidate in (
    _HERE.parents[2] / "src",                  # repo layout (examples/glyphs/.. /.. /src)
    _HERE.parent / "GlyphAudit-src",           # alt: bundled alongside script
):
    if (candidate / "GlyphAudit" / "__init__.py").exists():
        sys.path.insert(0, str(candidate))
        break

try:
    import tomllib  # 3.11+
except ImportError:
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        tomllib = None  # type: ignore

import vanilla
from GlyphsApp import Glyphs, UPDATEINTERFACE

from GlyphAudit.comparator import TieredComparator
from GlyphAudit.loaders import load_font
from GlyphAudit.model import (
    COLOR_FILTERS,
    GLYPHS_COLOR_NAMES,
    FontView,
    parse_variant_suffix,
)


CONFIG_PATH = Path.home() / ".glyph-audit" / "config.toml"


# ---------------------------------------------------------------------------
# Config → references map
# ---------------------------------------------------------------------------

def _load_references() -> dict[str, str]:
    """Return {master_name_lowercased: ref_spec_string} from the user config.

    Empty dict means the user hasn't configured `[instances.*]` yet.
    """
    if not CONFIG_PATH.exists() or tomllib is None:
        return {}
    try:
        with open(CONFIG_PATH, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return {}
    out: dict[str, str] = {}
    for name, entry in (data.get("instances") or {}).items():
        ref = entry.get("ref") if isinstance(entry, dict) else None
        if isinstance(ref, str):
            out[name.lower()] = ref
    return out


# ---------------------------------------------------------------------------
# Live in-memory FontView (avoids round-tripping through disk so the panel
# reflects unsaved edits)
# ---------------------------------------------------------------------------

def fontview_from_master(gs_font, master) -> FontView:
    cmap: dict[int, str] = {}
    advances: dict[str, int] = {}
    gsub_variants: dict[tuple[int, str], str] = {}
    all_names: set[str] = set()
    colors: dict[str, int] = {}

    for g in gs_font.glyphs:
        name = g.name
        all_names.add(name)
        if g.color is not None:
            colors[name] = g.color

        layer = g.layers[master.id]
        if layer is not None:
            advances[name] = int(round(layer.width))

        for u in (g.unicodes or []):
            try:
                cp = int(u, 16) if isinstance(u, str) else int(u)
                cmap[cp] = name
            except (TypeError, ValueError):
                continue

        parsed = parse_variant_suffix(name)
        if parsed:
            base, feature = parsed
            base_glyph = gs_font.glyphs[base]
            if base_glyph and base_glyph.unicodes:
                try:
                    cp = int(base_glyph.unicodes[0], 16)
                    gsub_variants[(cp, feature)] = name
                except (TypeError, ValueError):
                    pass

    return FontView(
        label=f"{gs_font.familyName} {master.name}",
        source=gs_font.filepath or "<unsaved>",
        source_kind="glyphs",
        upm=gs_font.upm,
        cmap=cmap,
        advances=advances,
        gsub_variants=gsub_variants,
        all_glyph_names=all_names,
        colors=colors,
    )


def _filter_for(target_view: FontView, key: str):
    if key == "all":
        return None, None
    cset = COLOR_FILTERS.get(key)
    if not cset:
        return None, None
    return (lambda name: target_view.colors.get(name) in cset), key


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

class WidthAuditPanel:
    _instance: "WidthAuditPanel | None" = None

    @classmethod
    def toggle(cls) -> None:
        if cls._instance is not None:
            cls._instance.close()
            return
        if Glyphs.font is None:
            Glyphs.showMacroWindow()
            print("Width Audit Panel: open a font first.")
            return
        cls._instance = cls()

    def __init__(self) -> None:
        self.font = Glyphs.font
        self.references = _load_references()
        self._live_subscribed = False

        master_names = [m.name for m in self.font.masters]
        self.w = vanilla.FloatingWindow(
            (480, 560),
            "Width Audit",
            autosaveName="GlyphAuditWidthPanel",
            minSize=(380, 240),
        )

        self.w.masterMenu = vanilla.PopUpButton(
            (10, 12, 110, 22), master_names, callback=self._refresh_cb
        )
        self.w.filterMenu = vanilla.PopUpButton(
            (130, 12, 110, 22),
            ["yellow", "ready", "all"],
            callback=self._refresh_cb,
        )
        self.w.liveBox = vanilla.CheckBox(
            (250, 12, 60, 22), "Live", value=True, callback=self._live_cb,
        )
        self.w.refreshBtn = vanilla.Button(
            (-90, 10, 80, 22), "Refresh", callback=self._refresh_cb
        )

        self.w.summary = vanilla.TextBox((10, 44, -10, 18), "")

        cols = [
            dict(title="Glyph", key="name", width=160, editable=False),
            dict(title="Tier", key="tier", width=40, editable=False),
            dict(title="Target", key="target", width=60, editable=False),
            dict(title="Ref", key="ref", width=60, editable=False),
            dict(title="Δ", key="delta", width=50, editable=False),
            dict(title="Color", key="color", width=70, editable=False),
        ]
        self.w.list = vanilla.List(
            (10, 70, -10, -10),
            [],
            columnDescriptions=cols,
            doubleClickCallback=self._open_glyph_cb,
            allowsMultipleSelection=False,
            autohidesScrollers=False,
        )

        self.w.bind("close", self._on_close)
        self._subscribe_live()
        self._refresh()
        self.w.open()

    # ----- lifecycle ------------------------------------------------------

    def close(self) -> None:
        try:
            self._unsubscribe_live()
            self.w.close()
        finally:
            WidthAuditPanel._instance = None

    def _on_close(self, sender) -> None:
        WidthAuditPanel._instance = None
        self._unsubscribe_live()

    # ----- live updates ---------------------------------------------------

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

    # ----- refresh --------------------------------------------------------

    def _refresh_cb(self, sender) -> None:
        self._refresh()

    def _refresh(self) -> None:
        if self.font is None or Glyphs.font is not self.font:
            self.font = Glyphs.font
        if self.font is None:
            self.w.summary.set("No font open.")
            self.w.list.set([])
            return

        master_idx = self.w.masterMenu.get()
        if master_idx >= len(self.font.masters):
            master_idx = 0
            self.w.masterMenu.set(0)
        master = self.font.masters[master_idx]

        ref_path = self.references.get(master.name.lower())
        if not ref_path:
            self.w.summary.set(
                f"No [instances.{master.name}] in {CONFIG_PATH}. "
                f"Add `ref = \"path/to/reference.ttf\"` and click Refresh."
            )
            self.w.list.set([])
            return

        try:
            ref_view = _load_reference(ref_path)
        except Exception as e:
            self.w.summary.set(f"Reference load failed: {e}")
            self.w.list.set([])
            return

        target_view = fontview_from_master(self.font, master)
        gfilter, filter_label = _filter_for(target_view, self.w.filterMenu.getItem())

        comp = TieredComparator(tolerance_units=1.0)
        result = comp.compare(
            target_view, ref_view,
            pair_label=master.name,
            target_filter=gfilter,
            filter_label=filter_label,
        )

        rows = []
        for r in result.codepoint_rows:
            if r.status != "mismatch":
                continue
            rows.append(self._row(r.target_name, "T1",
                                  r.target_advance, r.reference_advance,
                                  r.delta, target_view))
        for r in result.variant_rows:
            if r.status != "mismatch":
                continue
            rows.append(self._row(f"{r.target_name}  ({r.feature})", "T2",
                                  r.target_advance, r.reference_advance,
                                  r.delta, target_view, real_name=r.target_name))

        rows.sort(key=lambda r: abs(int(r["delta"] or 0)), reverse=True)

        counts = result.counts()
        self.w.summary.set(
            f"{len(rows)} mismatch{'es' if len(rows) != 1 else ''}  ·  "
            f"T1 {counts['tier1']['mismatch']}/{counts['tier1']['match'] + counts['tier1']['mismatch']}, "
            f"T2 {counts['tier2']['mismatch']}/{counts['tier2']['match'] + counts['tier2']['mismatch']}  ·  "
            f"filter={filter_label or 'all'}  ·  master={master.name}"
        )
        self.w.list.set(rows)

    def _row(self, display_name, tier, target_adv, ref_adv, delta, view, *, real_name=None):
        color_idx = view.colors.get(real_name or display_name)
        return dict(
            name=display_name,
            tier=tier,
            target=str(target_adv) if target_adv is not None else "—",
            ref=str(ref_adv) if ref_adv is not None else "—",
            delta=f"{delta:+.0f}" if delta is not None else "",
            color=GLYPHS_COLOR_NAMES.get(color_idx, ""),
            _name=real_name or display_name,
        )

    def _open_glyph_cb(self, sender) -> None:
        sel = sender.getSelection()
        if not sel or self.font is None:
            return
        item = sender.get()[sel[0]]
        name = item.get("_name") or item["name"].split(" ")[0]
        master_idx = self.w.masterMenu.get()
        try:
            # Switch the document to the master the row was audited against,
            # then open the glyph. Setting it on the returned tab too pins the
            # new edit view to that master if Glyphs ever decouples them.
            self.font.masterIndex = master_idx
            tab = self.font.newTab("/" + name)
            if tab is not None:
                try:
                    tab.masterIndex = master_idx
                except Exception:
                    pass
        except Exception:
            traceback.print_exc()


# Reference loading is cheap-but-not-free; cache across refreshes (and across
# live-update keystrokes — without this we'd re-parse the reference TTF on
# every glyph edit).
_ref_cache: dict[str, FontView] = {}


def _load_reference(path: str) -> FontView:
    cached = _ref_cache.get(path)
    if cached is not None:
        return cached
    view = load_font(path)
    _ref_cache[path] = view
    return view


# ---------------------------------------------------------------------------
# Run. Glyphs.app executes scripts with __name__ set to the file's basename
# (not "__main__"), so don't gate on that. Any exception surfaces in the
# Macro Window — Window → Macro Panel — instead of failing silently.
# ---------------------------------------------------------------------------

try:
    WidthAuditPanel.toggle()
except Exception:
    Glyphs.showMacroWindow()
    print("Width Audit Panel: failed to launch.")
    print(traceback.format_exc())
