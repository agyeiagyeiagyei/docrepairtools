"""Helpers for the Glyph Audit panel (and generic panel-state I/O).

Split from the old `common.py` along the break-out boundary: nothing in
this module imports from `GlyphAudit.proof.config` or knows about the
proof pipeline, so the audit panel + audit core (`model`, `comparator`,
`loaders`) can be extracted as a standalone package by moving files —
no surgery. Proof-only helpers (project-config discovery, subprocess
spawning) live in `proof_panel.py`, their sole consumer.

The generic JSON state helpers (`load_state` / `save_state`) live here
because the audit panel is their primary user; the proof panel borrows
them. If the proof side is ever split out, those ~15 lines get copied,
not re-architected.

State file:
    ~/.glyph-audit/audit-panel-state.json → recent references, filter,
                                            live toggle
"""

from __future__ import annotations

import json
import os
import traceback
from pathlib import Path

from vanilla.dialogs import getFile

from GlyphAudit.loaders import load_font
from GlyphAudit.model import COLOR_FILTERS, FontView, parse_variant_suffix

# Config read/write (AUDIT_CONFIG_PATH, template, [instances.*] loading,
# pin-to-master writing, editor-opening) lives in `configwrite.py` — a
# vanilla/AppKit-free module so it can be unit-tested outside Glyphs.
# Re-exported here so the audit panel keeps one import point.
from GlyphAudit.proof.panel.configwrite import (  # noqa: F401
    AUDIT_CONFIG_PATH,
    CONFIG_TEMPLATE,
    load_audit_references,
    open_config_in_editor,
    pin_reference_for_master,
)

AUDIT_STATE_PATH = Path.home() / ".glyph-audit" / "audit-panel-state.json"
MAX_RECENT_FILES = 8


# --------------------------------------------------------------------------
# State persistence (generic — proof panel borrows these)
# --------------------------------------------------------------------------

def load_state(path: Path) -> dict:
    """Load a panel's small JSON state file. Missing / malformed → {}."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def save_state(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2))
    except Exception:
        traceback.print_exc()


def load_recent_files(path: Path = AUDIT_STATE_PATH) -> list:
    state = load_state(path)
    files = state.get("recent_files") or []
    return [p for p in files if isinstance(p, str) and Path(p).is_file()]


def push_recent_file(new_path: str, state_path: Path = AUDIT_STATE_PATH) -> list:
    state = load_state(state_path)
    files = [p for p in (state.get("recent_files") or [])
             if isinstance(p, str) and p != new_path]
    files.insert(0, new_path)
    files = files[:MAX_RECENT_FILES]
    state["recent_files"] = files
    save_state(state_path, state)
    return files


# --------------------------------------------------------------------------
# FontView bridge — live in-memory view of the open Glyphs master
# --------------------------------------------------------------------------

def fontview_from_master(gs_font, master) -> FontView:
    cmap = {}
    advances = {}
    lsbs = {}
    rsbs = {}
    gsub_variants = {}
    all_names = set()
    colors = {}

    for g in gs_font.glyphs:
        name = g.name
        all_names.add(name)
        if g.color is not None:
            colors[name] = g.color

        layer = g.layers[master.id]
        if layer is not None:
            advances[name] = int(round(layer.width))
            # Glyphs.app exposes LSB/RSB as layer properties (auto-computed
            # from bbox for empty glyphs). Missing on very old .glyphspackage
            # formats — tolerate that so the panel still renders advances.
            try:
                lsbs[name] = int(round(layer.LSB))
            except (AttributeError, TypeError):
                pass
            try:
                rsbs[name] = int(round(layer.RSB))
            except (AttributeError, TypeError):
                pass

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

    # Glyphs' AUTOMATIC frac feature routes slash through a glyph named
    # "fraction" whenever one exists — no .suffix convention involved.
    # Mirror that here or the coverage panel flags the glyph as
    # present-unlinked even though the compiled font will contain
    # `sub slash by fraction;` (verified against generated feature code).
    if "fraction" in all_names and 0x2F in cmap:
        gsub_variants.setdefault((0x2F, "frac"), "fraction")

    return FontView(
        label=f"{gs_font.familyName} {master.name}",
        source=gs_font.filepath or "<unsaved>",
        source_kind="glyphs",
        upm=gs_font.upm,
        cmap=cmap,
        advances=advances,
        left_sidebearings=lsbs,
        right_sidebearings=rsbs,
        gsub_variants=gsub_variants,
        all_glyph_names=all_names,
        colors=colors,
    )


def filter_for(target_view, key):
    if key == "all":
        return None, None
    cset = COLOR_FILTERS.get(key)
    if not cset:
        return None, None
    return (lambda name: target_view.colors.get(name) in cset), key


# --------------------------------------------------------------------------
# Reference picker — user-supplied files + config entries only.
# System-font enumeration intentionally absent: `System · Verdana` on macOS
# resolves to whichever redistribution is installed, and Apple's Verdana
# ships without the smcp / c2sc GSUB features Microsoft's carries. Pinned
# files avoid that ambiguity. (`matching.py` + its tests stay, free-standing,
# in case system lookup returns.)
# --------------------------------------------------------------------------

def pick_font_file_async(parent_window, on_picked, on_cancelled=None) -> None:
    """Open a sheet-modal file picker attached to `parent_window`. Delivers
    the picked path to `on_picked(path)` asynchronously.
    """
    def _result(paths):
        if not paths:
            if on_cancelled:
                on_cancelled()
            return
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


# --------------------------------------------------------------------------
# Reference loading (cached across refreshes)
# --------------------------------------------------------------------------

_ref_cache: dict = {}
# Parallel cache of raw fontTools TTFont handles keyed by resolved file path
# — used ONLY by the edit-view overlay, which needs contours (FontView only
# carries metrics). Kept separate from `_ref_cache` because it's opt-in.
_ttfont_cache: dict = {}


def load_reference_cached(path: str) -> FontView:
    cached = _ref_cache.get(path)
    if cached is not None:
        return cached
    view = load_font(path)
    _ref_cache[path] = view
    return view


def _resolve_ref_file_path(ref_view) -> str:
    """Extract the underlying filesystem path from a loaded reference
    FontView. `source` may carry a `"@wght=400"` axis-pin suffix (strip it)
    or, for legacy system picks, a `"Family Style (/path.ttf)"` wrapper.
    """
    src = getattr(ref_view, "source", "") or ""
    if getattr(ref_view, "source_kind", "") == "system":
        if "(" in src and src.endswith(")"):
            return src.rsplit("(", 1)[-1].rstrip(")")
        return ""
    return src.split("@", 1)[0]


def ttfont_for(ref_view):
    """Return a fontTools TTFont for the given reference FontView, or None
    if the underlying file can't be loaded. Cached per-path.
    """
    path = _resolve_ref_file_path(ref_view)
    if not path or not os.path.isfile(path):
        return None
    cached = _ttfont_cache.get(path)
    if cached is not None:
        return cached
    try:
        from fontTools.ttLib import TTFont
    except ImportError:
        return None
    try:
        ttfont = TTFont(path)
    except Exception:
        return None
    _ttfont_cache[path] = ttfont
    return ttfont
