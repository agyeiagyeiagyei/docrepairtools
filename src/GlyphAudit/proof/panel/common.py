"""Shared helpers for both Glyph Proof and Width Audit panels.

Design note — why two panels share a module and not a class:
Both panels are independently open-able and both need the same
utilities (config discovery, state I/O, FontView bridge, reference
picker, PATH-fixing subprocess helpers). Rather than build an
inheritance tree, each panel imports what it needs from here. Nothing
in this module owns Cocoa UI — it's pure Python helpers plus vanilla
sheet dialogs and fontTools TTF loading.

State files are keyed per-panel so opening one doesn't clobber the
other:
    ~/.glyph-audit/proof-panel-state.json    → proof colours, sources
    ~/.glyph-audit/audit-panel-state.json    → recent references, filter,
                                                live toggle
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Optional

try:
    import tomllib  # 3.11+
except ImportError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        tomllib = None  # type: ignore

from vanilla.dialogs import getFile

from GlyphAudit.loaders import load_font
from GlyphAudit.model import COLOR_FILTERS, FontView, parse_variant_suffix
from GlyphAudit.proof.config import ConfigError, load_project_config


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

# Config read/write (AUDIT_CONFIG_PATH, template, [instances.*] loading,
# pin-to-master writing, editor-opening) lives in `configwrite.py` — a
# vanilla/AppKit-free module so it can be unit-tested outside Glyphs.
# Re-exported here so the panels keep one import point.
from GlyphAudit.proof.panel.configwrite import (  # noqa: F401
    AUDIT_CONFIG_PATH,
    CONFIG_TEMPLATE,
    load_audit_references,
    open_config_in_editor,
    pin_reference_for_master,
)

# Per-panel state paths. Kept split so writes from one panel don't rewrite
# the other's file on every checkbox change.
PROOF_STATE_PATH = Path.home() / ".glyph-audit" / "proof-panel-state.json"
AUDIT_STATE_PATH = Path.home() / ".glyph-audit" / "audit-panel-state.json"

MAX_RECENT_FILES = 8

DEV_SERVER_URL = "http://localhost:5173"

# `SYSTEM_SUFFIX` constant kept in `loaders.py` where the -system-lookup
# path still lives; the panel just doesn't offer that path anymore.


# --------------------------------------------------------------------------
# State persistence
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
# Project discovery (glyph-audit.toml)
# --------------------------------------------------------------------------

def project_config_for(font):
    """Locate `glyph-audit.toml` starting from the active font's file path.

    Returns (ProjectConfig, None) on success, (None, reason) on failure.
    Reason is either "unsaved document …", "no glyph-audit.toml found …",
    or a ConfigError message.
    """
    fp = getattr(font, "filepath", None)
    if not fp:
        return None, "unsaved document — save the font first"
    start = Path(fp).resolve().parent
    try:
        cfg = load_project_config(start)
    except ConfigError as e:
        return None, str(e)
    if cfg is None:
        return None, "no glyph-audit.toml found up the tree"
    return cfg, None


# --------------------------------------------------------------------------
# FontView bridge — used by the width-audit panel
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
# Reference picker — system fonts, files, config entries
# --------------------------------------------------------------------------

# System-font picker helpers (system_font_families, system_family_members,
# resolve_system_reference) intentionally removed — the audit panel now
# accepts only user-supplied references (file picker + Config entries).
# Reason: `System · Verdana` on macOS resolves to whichever redistribution
# happens to be installed, and Apple's Verdana ships without the smcp /
# c2sc GSUB features Microsoft's does. Users pinning a specific TTF via
# the file picker or `[instances.*]` avoids that ambiguity entirely.
#
# The `matching.py` module and its 46 pure-Python tests stay — they're
# free-standing and useful if we re-enable system fonts later.


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
# carries metrics). Kept separate from `_ref_cache` because it's opt-in
# (loading the whole `glyf` table for every reference the user browses
# would waste memory when they're not using the overlay).
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
    FontView. Two shapes to handle:

      - System pick: `source == "Family Style (/absolute/path.ttf)"`
      - Direct file: `source == "/absolute/path.ttf"` (may carry a
        `"@wght=400"` axis-pin suffix — strip it, the on-disk file
        is what fontTools needs).
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


# --------------------------------------------------------------------------
# Subprocess helpers (Glyphs.app has a stripped PATH — the panel spawns
# `glyph-audit` and needs to inject the user's shell PATH so downstream
# tools like fontc are findable)
# --------------------------------------------------------------------------

def login_shell_path() -> str:
    """Return the PATH your Terminal would see, or empty string if the
    login-shell probe fails.
    """
    for shell in ("/bin/zsh", "/bin/bash"):
        if not os.path.exists(shell):
            continue
        try:
            result = subprocess.run(
                [shell, "-l", "-c", "echo $PATH"],
                capture_output=True, text=True, timeout=3,
            )
        except (subprocess.TimeoutExpired, OSError):
            continue
        path = (result.stdout or "").strip().splitlines()[-1:] or [""]
        path = path[0].strip()
        if path:
            return path
    return ""


def find_glyph_audit_cli() -> list:
    """Locate the `glyph-audit` executable, working around Glyphs.app's
    embedded Python not inheriting the user's shell PATH. Order:

        1. PATH lookup
        2. Login-shell `command -v`
        3. Common install locations
        4. sys.executable + `-m` (only if it's clearly a Python binary,
           NEVER the Glyphs.app bundle itself)

    Raises RuntimeError with an actionable message on total failure.
    """
    direct = shutil.which("glyph-audit")
    if direct:
        return [direct]

    for shell in ("/bin/zsh", "/bin/bash"):
        if not os.path.exists(shell):
            continue
        try:
            result = subprocess.run(
                [shell, "-l", "-c", "command -v glyph-audit"],
                capture_output=True, text=True, timeout=3,
            )
        except (subprocess.TimeoutExpired, OSError):
            continue
        candidate = (result.stdout or "").strip().splitlines()[-1:] or [""]
        candidate = candidate[0].strip()
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return [candidate]

    for candidate in (
        "/usr/local/bin/glyph-audit",
        "/opt/homebrew/bin/glyph-audit",
        "/Library/Frameworks/Python.framework/Versions/Current/bin/glyph-audit",
        os.path.expanduser("~/.local/bin/glyph-audit"),
        os.path.expanduser("~/Library/Python/3.11/bin/glyph-audit"),
        os.path.expanduser("~/Library/Python/3.12/bin/glyph-audit"),
    ):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return [candidate]

    exe = sys.executable or ""
    exe_base = os.path.basename(exe).lower()
    is_python = exe_base.startswith("python") and ".app/" not in exe
    if is_python:
        return [exe, "-m", "GlyphAudit"]

    raise RuntimeError(
        "Couldn't locate `glyph-audit` from Glyphs.app. "
        "Install with `pip install docrepair-glyph-audit`, then make sure "
        "the install dir is in your shell PATH (~/.zshrc or ~/.bash_profile). "
        f"Tried: PATH, login shell lookup, common install locations, and "
        f"sys.executable ({exe!r})."
    )


# sys.path bootstrap lives in `_bootstrap.py` — it must have zero
# GlyphAudit imports so it can run before this module (which imports
# from GlyphAudit.loaders / .model / .proof.config at the top).
