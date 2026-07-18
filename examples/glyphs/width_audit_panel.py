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

import json
import subprocess
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
from vanilla.dialogs import getFile
from AppKit import NSFontManager
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
STATE_PATH  = Path.home() / ".glyph-audit" / "width-panel-state.json"
MAX_RECENT_FILES = 8

CONFIG_TEMPLATE = """\
# GlyphAudit config — opened by the Width Audit Panel's "Edit config…" button.
#
# Each [instances.NAME] entry maps a Glyphs master name (case-insensitive)
# to a reference font. The panel picks the entry matching the current
# master by default; you can override it from the Reference dropdown.
#
# Reference forms — any of these work as `ref`:
#   - Static TTF / OTF on disk
#   - Variable font on disk + axis pin (use the CLI form for axis pinning)
#   - "Family-system" to use a system-installed font directly
#   - Glyphs source file (.glyphspackage / .glyphs)
#
# Examples:

# [instances.Regular]
# ref = "/Users/me/fonts/Reference-Regular.ttf"

# [instances.Bold]
# ref = "/Users/me/fonts/Reference-Bold.ttf"
"""


# ---------------------------------------------------------------------------
# Config → references map
# ---------------------------------------------------------------------------

def _load_panel_state() -> dict:
    """Load the panel's small JSON state (currently just the recent-files
    list). Tolerates missing / malformed file by returning {}."""
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def _save_panel_state(state: dict) -> None:
    """Persist `state` to ~/.glyph-audit/width-panel-state.json."""
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, indent=2))
    except Exception:
        traceback.print_exc()


def _load_recent_files() -> list[str]:
    """Recent references the user has picked via `Choose file…`, most recent
    first. Stale paths (deleted files) are filtered out."""
    state = _load_panel_state()
    files = state.get("recent_files") or []
    return [p for p in files if isinstance(p, str) and Path(p).is_file()]


def _push_recent_file(path: str) -> list[str]:
    """Move `path` to the front of the recent list, dedupe, cap, persist.
    Returns the resulting list."""
    state = _load_panel_state()
    files = [p for p in (state.get("recent_files") or []) if isinstance(p, str) and p != path]
    files.insert(0, path)
    files = files[:MAX_RECENT_FILES]
    state["recent_files"] = files
    _save_panel_state(state)
    return files


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
# Reference picker sources: config / system / file
# ---------------------------------------------------------------------------

def _system_font_families() -> list[str]:
    """Every installed font family the OS exposes via NSFontManager.
    Returns alphabetically sorted list; empty if AppKit isn't available
    (Glyphs's Python env always has it, so this is just paranoia)."""
    try:
        return sorted(NSFontManager.sharedFontManager().availableFontFamilies())
    except Exception:
        return []


def _pick_font_file_async(parent_window, on_picked, on_cancelled=None) -> None:
    """Open a sheet-modal file picker attached to `parent_window`.

    Async: the chosen path is delivered to `on_picked(path)` on success, or
    `on_cancelled()` if the user dismissed the sheet. We can't return the
    result synchronously because vanilla's synchronous `getFile` path uses
    `NSOpenPanel.runModalForDirectory_file_types_` — Apple deprecated that
    in 10.6 and it stopped showing any dialog at all on macOS 13+. The
    sheet-modal form (`beginSheetModalForWindow_completionHandler_`) still
    works, and vanilla wraps it for us when we pass `parentWindow`.
    """
    def _result(paths):
        # vanilla hands us an NSArray of NSCFString — coerce to a plain
        # Python list of POSIX strings so downstream `open(...)` /
        # `load_font(...)` calls don't choke on the wrapper type.
        if not paths:
            if on_cancelled:
                on_cancelled()
            return
        # Single-select dialog; first entry is the pick.
        path = str(paths[0]) if hasattr(paths, "__getitem__") else str(paths)
        on_picked(path)

    def _cancel():
        if on_cancelled:
            on_cancelled()

    getFile(
        messageText="Choose reference font",
        fileTypes=("ttf", "otf", "ttc", "TTF", "OTF", "TTC"),
        allowsMultipleSelection=False,
        parentWindow=parent_window,
        resultCallback=_result,
        cancelCallback=_cancel,
    )


def _open_config_in_editor() -> None:
    """Open ~/.glyph-audit/config.toml in the user's default editor.
    Creates the directory + a starter template if the file doesn't exist."""
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    try:
        subprocess.run(["open", str(CONFIG_PATH)], check=False)
    except Exception:
        traceback.print_exc()


# Option-list spec used by the Reference dropdown:
#   ("config",      master_name_lc)   — entry from [instances.*]
#   ("system",      family_name)      — macOS-installed font
#   ("file",        absolute_path)    — last-picked file (sticky for session)
#   ("file_picker", None)             — sentinel that opens the file dialog
#   ("sep",         label)            — visual separator row (selectable but ignored)


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
        self._live_subscribed = False
        # Reference-picker state. The dropdown is built from a parallel array
        # of (kind, identifier) tuples in `self._option_specs` — see
        # `_rebuild_reference_menu` for the full grammar. `_user_picked`
        # tracks whether the user has manually overridden the default
        # config-match-by-master selection. Picked files persist between
        # sessions via `width-panel-state.json`.
        self._option_specs: list[tuple[str, object]] = []
        self._user_picked_index: int | None = None

        master_names = [m.name for m in self.font.masters]
        self.w = vanilla.FloatingWindow(
            (520, 600),
            "Width Audit",
            autosaveName="GlyphAuditWidthPanel",
            minSize=(420, 280),
        )

        # ----- Row 1: master / filter / live / refresh -----
        self.w.masterMenu = vanilla.PopUpButton(
            (10, 12, 110, 22), master_names, callback=self._master_changed_cb,
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
            (-90, 10, 80, 22), "Refresh", callback=self._refresh_cb,
        )

        # ----- Row 2: reference picker + edit-config -----
        self.w.refLabel = vanilla.TextBox(
            (10, 46, 70, 18), "Reference:", sizeStyle="small",
        )
        self.w.refMenu = vanilla.PopUpButton(
            (80, 42, -110, 22), [], callback=self._reference_picked_cb,
        )
        self.w.editConfigBtn = vanilla.Button(
            (-100, 40, 90, 22), "Edit config…",
            callback=lambda sender: self._edit_config_cb(),
        )

        self.w.summary = vanilla.TextBox((10, 76, -10, 18), "", sizeStyle="small")

        cols = [
            dict(title="Glyph", key="name", width=160, editable=False),
            dict(title="Tier", key="tier", width=40, editable=False),
            dict(title="Target", key="target", width=60, editable=False),
            dict(title="Ref", key="ref", width=60, editable=False),
            dict(title="Δ", key="delta", width=50, editable=False),
            dict(title="Color", key="color", width=70, editable=False),
        ]
        self.w.list = vanilla.List(
            (10, 102, -10, -10),
            [],
            columnDescriptions=cols,
            doubleClickCallback=self._open_glyph_cb,
            allowsMultipleSelection=False,
            autohidesScrollers=False,
        )

        self.w.bind("close", self._on_close)
        self._rebuild_reference_menu(select_master_default=True)
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

    # ----- reference picker -----------------------------------------------

    def _rebuild_reference_menu(self, *, select_master_default: bool) -> None:
        """Rebuild the Reference dropdown from current config + recents +
        system fonts. Called on init and whenever the user clicks
        Edit config… (so newly added [instances.*] entries appear) or
        picks a file (so the recent list updates).

        No visual separators — macOS's NSPopUpButton silently converts long
        dash runs to inert separator rows, which throws off the click→index
        mapping. Distinct prefixes (Config / Recent / System) keep the
        groups visually obvious without needing dividers.
        """
        items: list[str] = []
        specs: list[tuple[str, object]] = []

        # Picker action first — easiest to find, no scrolling needed.
        items.append("Choose file…")
        specs.append(("file_picker", None))

        for master_lc, ref_path in _load_references().items():
            short = Path(ref_path).name if "/" in ref_path else ref_path
            items.append(f"Config · {master_lc} → {short}")
            specs.append(("config", master_lc))

        recents = _load_recent_files()
        for path in recents:
            items.append(f"Recent · {Path(path).name}")
            specs.append(("file", path))

        for family in _system_font_families():
            items.append(f"System · {family}")
            specs.append(("system", family))

        self._option_specs = specs
        self.w.refMenu.setItems(items)

        if select_master_default:
            self._select_default_for_master()

    def _select_default_for_master(self) -> None:
        """Pick the config entry that matches the current master if any;
        otherwise leave whatever the user last selected, or pick the file
        picker as the fallback if nothing has been chosen yet."""
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
        # No config match. If the user already picked something, keep that.
        if self._user_picked_index is not None and self._user_picked_index < len(self._option_specs):
            self.w.refMenu.set(self._user_picked_index)
            return
        # Otherwise fall back to the file picker prompt so the user sees
        # immediately that no reference is wired up.
        for i, (kind, _) in enumerate(self._option_specs):
            if kind == "file_picker":
                self.w.refMenu.set(i)
                return

    def _reference_picked_cb(self, sender) -> None:
        idx = sender.get()
        if idx < 0 or idx >= len(self._option_specs):
            return
        kind, ident = self._option_specs[idx]
        if kind == "sep":
            # Snap back to the previous valid selection.
            if self._user_picked_index is not None:
                self.w.refMenu.set(self._user_picked_index)
            else:
                self._select_default_for_master()
            return
        if kind == "file_picker":
            # File picker is async (sheet-modal). Revert the dropdown to its
            # previous valid selection right now so the user sees something
            # consistent while the sheet is open; apply the file when (and
            # only if) the sheet returns a path.
            self._restore_previous_selection()
            try:
                parent = self.w.getNSWindow()
            except Exception:
                parent = None
            _pick_font_file_async(
                parent,
                on_picked=self._apply_picked_file,
                on_cancelled=None,  # dropdown already reverted; nothing to do
            )
            return
        # Plain config / system / file pick — record the override so master
        # changes don't yank the user back to the config default unless they
        # actually want that.
        self._user_picked_index = idx
        self._refresh()

    def _restore_previous_selection(self) -> None:
        if self._user_picked_index is not None and self._user_picked_index < len(self._option_specs):
            self.w.refMenu.set(self._user_picked_index)
        else:
            self._select_default_for_master()

    def _apply_picked_file(self, path: str) -> None:
        """Called by the async file picker once the user actually chose a file.
        Pushes the path to the persisted recents list so it survives panel
        toggles and Glyphs restarts."""
        _push_recent_file(path)
        self._rebuild_reference_menu(select_master_default=False)
        for i, (k, ide) in enumerate(self._option_specs):
            if k == "file" and ide == path:
                self.w.refMenu.set(i)
                self._user_picked_index = i
                break
        self._refresh()

    def _edit_config_cb(self) -> None:
        _open_config_in_editor()
        # User will edit + save in their editor; rebuilding the menu on the
        # next refresh tick picks up additions automatically.
        self._rebuild_reference_menu(select_master_default=True)
        self._refresh()

    def _master_changed_cb(self, sender) -> None:
        # On master switch, default to whichever config entry matches the new
        # master. The user can still override via the Reference dropdown.
        self._select_default_for_master()
        self._refresh()

    def _resolve_reference_spec(self) -> tuple[str | None, str | None]:
        """Return (load_font_spec, human_label) for the currently-selected ref.
        load_font_spec is None when no usable reference is wired up.
        """
        idx = self.w.refMenu.get()
        if idx < 0 or idx >= len(self._option_specs):
            return None, None
        kind, ident = self._option_specs[idx]
        if kind == "config":
            ref = _load_references().get(ident)
            return ref, f"config · {ident}"
        if kind == "system":
            return f"{ident}-system", f"system · {ident}"
        if kind == "file":
            return ident, f"file · {Path(ident).name}"
        return None, None  # sep / file_picker shouldn't get here

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

        ref_spec, ref_label = self._resolve_reference_spec()
        if not ref_spec:
            self.w.summary.set(
                "No reference selected. Pick a system font, "
                "choose a file, or click Edit config… to add an [instances.*] entry."
            )
            self.w.list.set([])
            return

        try:
            ref_view = _load_reference(ref_spec)
        except Exception as e:
            # Show a short summary in the panel and a full traceback in the
            # Macro Window so the user can paste the actual failure into a
            # bug report. Includes the resolved spec so it's clear *what*
            # the loader was asked to open.
            self.w.summary.set(
                f"Reference load failed ({ref_label}): {type(e).__name__}: {e}"
            )
            self.w.list.set([])
            Glyphs.showMacroWindow()
            print(f"Width Audit Panel — reference load failed")
            print(f"  spec: {ref_spec!r}")
            print(f"  label: {ref_label}")
            traceback.print_exc()
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
            f"filter={filter_label or 'all'}  ·  master={master.name}  ·  ref={ref_label}"
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
