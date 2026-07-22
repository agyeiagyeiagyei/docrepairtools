"""`glyph-audit proof …` subcommands.

Kept in a separate module from `cli.py` so the audit-only surface loads
without pulling in fontc / watchdog / http.server imports until the user
actually invokes the proof pipeline. Each subcommand is one function
that consumes the parsed `args` object and returns a Unix-style exit
code.

Discovery: every subcommand loads the project's TOML config via
`GlyphAudit.proof.config.load_project_config`, which walks up from CWD
looking for `glyph-audit.toml` (or `.glyph-audit.toml`). Missing config
is a fatal error for `build/watch/serve` — those commands need to know
what to build and what to compare against — but `panel install` works
without one.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _resolve_config(config_arg: Optional[str]):
    """Load the project config or exit with a clear message.

    If the user passed `--config PATH`, that file must exist and parse.
    Otherwise we walk up from CWD looking for `glyph-audit.toml`.
    """
    from .proof.config import ConfigError, load_project_config

    try:
        if config_arg:
            path = Path(config_arg).expanduser().resolve()
            if not path.is_file():
                print(f"FAIL: config file not found: {path}", file=sys.stderr)
                sys.exit(2)
            cfg = load_project_config(path.parent)
            if cfg is None or cfg.config_path != path:
                # `load_project_config` looks by filename; if the user pointed
                # at a differently-named file we still want to honour it.
                # Fall through to a direct parse.
                import tomllib
                data = tomllib.loads(path.read_text())
                from .proof.config import _parse_proof
                proof = _parse_proof(path.parent, data)
                from .proof.config import ProjectConfig
                cfg = ProjectConfig(
                    project_root=path.parent,
                    config_path=path,
                    name=(data.get("project") or {}).get("name"),
                    proof=proof,
                )
        else:
            cfg = load_project_config(Path.cwd())
    except ConfigError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(2)

    if cfg is None:
        print(
            "FAIL: no glyph-audit.toml found. Create one at your project root, "
            "or pass --config PATH.\n"
            "See https://github.com/agyeiagyeiagyei/docrepairtools#proof for the schema.",
            file=sys.stderr,
        )
        sys.exit(2)
    return cfg


def _override_colors(cfg, colors_arg: Optional[str]):
    """Return the effective color set: CLI override → config → default."""
    from .proof.config import validate_colors

    if not colors_arg:
        return cfg.proof.colors
    parts = [p.strip().lower() for p in colors_arg.split(",") if p.strip()]
    try:
        return validate_colors(parts)
    except ValueError as e:
        print(f"FAIL: --colors: {e}", file=sys.stderr)
        sys.exit(2)


def _resolved_sources(cfg, sources_arg):
    """CLI --source overrides config. Both are basenames relative to
    `<project_root>/sources/`; we resolve to absolute paths here so the
    build library gets what it expects."""
    src_names = list(sources_arg) if sources_arg else list(cfg.proof.sources)
    sources_dir = cfg.project_root / "sources"
    return [str(sources_dir / s) for s in src_names], src_names


def _resolved_output_dir(cfg) -> str:
    """Absolute path for the build's output directory. Config value is
    relative to project root unless absolute."""
    out = Path(cfg.proof.output_dir)
    if not out.is_absolute():
        out = cfg.project_root / out
    return str(out)


# ---------------------------------------------------------------------------
# Subcommand: proof build
# ---------------------------------------------------------------------------

def cmd_build(args) -> int:
    """One-shot build of every configured source."""
    import shutil as _shutil
    from .proof import build_proof_font, write_proof_config, write_width_manifests

    # Pre-flight: fontc is a hard runtime dep of the build path, and its
    # absence otherwise surfaces as a subprocess.FileNotFoundError halfway
    # through the build — cryptic. Fail early with an actionable message.
    if _shutil.which("fontc") is None:
        print(
            "FAIL: `fontc` not found on PATH.\n"
            "  Install with:  pip install \"docrepair-tools[proof]\"\n"
            "  Or directly:   pip install fontc",
            file=sys.stderr,
        )
        return 2

    cfg = _resolve_config(args.config)
    colors = _override_colors(cfg, args.colors)
    src_paths, src_names = _resolved_sources(cfg, args.source)
    output_dir = _resolved_output_dir(cfg)

    any_failed = False
    for src in src_paths:
        ok = build_proof_font(
            source_path=src,
            output_dir=output_dir,
            output_basename=cfg.proof.output_basename,
            proof_colors=colors,
            essential_glyphs=cfg.proof.essential_glyphs,
        )
        any_failed = any_failed or not ok

    # Runtime manifests for the web app. Written unconditionally so a partial
    # build still gives the app *something* to render; and after the TTFs
    # exist so `write_width_manifests` can measure them.
    write_proof_config(
        output_dir=output_dir,
        family_name=cfg.proof.family_name,
        output_basename=cfg.proof.output_basename,
        sources=src_names,
        references=cfg.proof.references,
    )
    write_width_manifests(
        output_dir=output_dir,
        output_basename=cfg.proof.output_basename,
        sources=src_names,
        references=cfg.proof.references,
    )

    return 1 if any_failed else 0


# ---------------------------------------------------------------------------
# Subcommand: proof watch
# ---------------------------------------------------------------------------

def cmd_watch(args) -> int:
    """Build once, then keep rebuilding as sources change."""
    from .proof import (
        build_proof_font, watch_and_rebuild,
        write_proof_config, write_width_manifests,
    )

    cfg = _resolve_config(args.config)
    colors = _override_colors(cfg, args.colors)
    src_paths, src_names = _resolved_sources(cfg, args.source)
    output_dir = _resolved_output_dir(cfg)

    # Initial build so the web app has something to fetch immediately.
    for src in src_paths:
        build_proof_font(
            source_path=src,
            output_dir=output_dir,
            output_basename=cfg.proof.output_basename,
            proof_colors=colors,
            essential_glyphs=cfg.proof.essential_glyphs,
        )
    write_proof_config(
        output_dir=output_dir,
        family_name=cfg.proof.family_name,
        output_basename=cfg.proof.output_basename,
        sources=src_names,
        references=cfg.proof.references,
    )
    write_width_manifests(
        output_dir=output_dir,
        output_basename=cfg.proof.output_basename,
        sources=src_names,
        references=cfg.proof.references,
    )

    watch_and_rebuild(
        source_paths=src_paths,
        output_dir=output_dir,
        output_basename=cfg.proof.output_basename,
        proof_colors=colors,
        essential_glyphs=cfg.proof.essential_glyphs,
    )
    return 0


# ---------------------------------------------------------------------------
# Subcommand: proof serve
# ---------------------------------------------------------------------------

def cmd_serve(args) -> int:
    """Build + serve the packaged web app. Optionally watch for changes."""
    from .proof import server

    cfg = _resolve_config(args.config)
    output_dir = _resolved_output_dir(cfg)

    # Build first so the overlay has something meaningful to serve.
    if not args.no_build:
        rc = cmd_build(args)
        if rc != 0 and not args.watch:
            return rc

    if args.watch:
        # In watch mode we background the watcher and foreground the server.
        # The watcher's stdout streams to our stdout so build errors are
        # visible; SIGINT to the server propagates via the process group.
        import threading
        from .proof import watch_and_rebuild
        src_paths, _ = _resolved_sources(cfg, args.source)
        colors = _override_colors(cfg, args.colors)

        def _watch():
            try:
                watch_and_rebuild(
                    source_paths=src_paths,
                    output_dir=output_dir,
                    output_basename=cfg.proof.output_basename,
                    proof_colors=colors,
                    essential_glyphs=cfg.proof.essential_glyphs,
                )
            except Exception as e:
                print(f"[watch] crashed: {e}", file=sys.stderr)

        t = threading.Thread(target=_watch, daemon=True)
        t.start()

    server.serve(
        output_dir=output_dir,
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
    )
    return 0


# ---------------------------------------------------------------------------
# Subcommand: proof panel install
# ---------------------------------------------------------------------------

_GLYPHS_SCRIPTS_DIR = Path.home() / "Library/Application Support/Glyphs 3/Scripts"

# A folder inside Glyphs's Scripts dir renders as a SUBMENU — so the two
# tools install as one "DocRepair Tools" entry with dropdown items,
# rather than two loose top-level scripts.
_MENU_DIR = _GLYPHS_SCRIPTS_DIR / "DocRepair Tools"
_PANELS = (
    ("Glyph Audit.py", "audit_panel.py"),
    ("Make Proof.py", "proof_panel.py"),
)

# Superseded top-level symlinks from earlier generations of the installer.
# Removed on every install so upgrades don't leave duplicate menu items.
_LEGACY_LINKS = (
    "Glyph Proof.py",
    "Width Audit.py",
    "Velarium Proof.py",
    "Width Audit Panel.py",
)


def cmd_panel_install(args) -> int:
    """Install the DocRepair Tools submenu into Glyphs 3's user-scripts
    folder: Script → DocRepair Tools → {Glyph Audit, Make Proof}.
    Replaces any existing links and removes superseded top-level ones.
    """
    from importlib import resources

    try:
        pkg_ref = resources.files("GlyphAudit.proof.panel")
    except (ModuleNotFoundError, FileNotFoundError):
        print(
            "FAIL: panel module not found in this GlyphAudit install. "
            "This can happen with older or partial checkouts.",
            file=sys.stderr,
        )
        return 2

    try:
        _MENU_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"FAIL: could not create {_MENU_DIR}: {e}", file=sys.stderr)
        return 2

    for legacy in _LEGACY_LINKS:
        stale = _GLYPHS_SCRIPTS_DIR / legacy
        if stale.exists() or stale.is_symlink():
            stale.unlink()
            print(f"Removed superseded link: {stale.name}")

    for menu_name, filename in _PANELS:
        panel_ref = pkg_ref.joinpath(filename)
        with resources.as_file(panel_ref) as p:
            src = Path(p).resolve()
        if not src.is_file():
            print(f"FAIL: panel file not found at {src}", file=sys.stderr)
            return 2

        dest = _MENU_DIR / menu_name
        if dest.exists() or dest.is_symlink():
            dest.unlink()
        dest.symlink_to(src)
        print(f"Symlinked: {dest} → {src}")

    print("In Glyphs: hold Option and click Script → Reload Scripts (or relaunch).")
    print("Menu: Script → DocRepair Tools → Glyph Audit · Make Proof")
    return 0


# ---------------------------------------------------------------------------
# Argparse wiring
# ---------------------------------------------------------------------------

def add_proof_subparser(parent_subparsers) -> argparse.ArgumentParser:
    """Attach the `proof` subcommand tree onto the top-level `subparsers`."""
    proof = parent_subparsers.add_parser(
        "proof",
        help="Build + serve the proof web app.",
        description=(
            "Compile a subset proof TTF from ready-flagged glyphs and (with "
            "`serve`) launch a side-by-side browser view against configured "
            "reference fonts. Reads `glyph-audit.toml` from the project root."
        ),
    )
    sub = proof.add_subparsers(dest="proof_cmd", required=True)

    def add_common(p):
        p.add_argument("--config", default=None,
                       help="Path to glyph-audit.toml (default: discovered from CWD).")
        p.add_argument("--source", action="append", default=None,
                       help="Override the source list from config. Repeatable.")
        p.add_argument("--colors", default=None,
                       help="Comma-separated Glyphs.app color keys (0..11 or `none`). "
                            "Overrides [proof].colors from config.")

    build_p = sub.add_parser("build", help="One-shot compile of all sources.")
    add_common(build_p)
    build_p.set_defaults(func=cmd_build)

    watch_p = sub.add_parser("watch", help="Build once, then rebuild on source changes.")
    add_common(watch_p)
    watch_p.set_defaults(func=cmd_watch)

    serve_p = sub.add_parser(
        "serve",
        help="Build + serve the shipped web app (adds --watch for live rebuild).",
    )
    add_common(serve_p)
    serve_p.add_argument("--host", default="127.0.0.1",
                         help="Bind address (default: 127.0.0.1).")
    serve_p.add_argument("--port", type=int, default=5173,
                         help="Port (default: 5173, matching Vite's dev-server default).")
    serve_p.add_argument("--no-browser", action="store_true",
                         help="Skip auto-opening the browser on start.")
    serve_p.add_argument("--no-build", action="store_true",
                         help="Skip the initial build. Useful when the output dir is already fresh.")
    serve_p.add_argument("--watch", action="store_true",
                         help="Also spawn a watcher so edits rebuild live.")
    serve_p.set_defaults(func=cmd_serve)

    panel_p = sub.add_parser("panel", help="Manage the Glyphs.app panel install.")
    panel_sub = panel_p.add_subparsers(dest="panel_cmd", required=True)
    panel_install = panel_sub.add_parser("install", help="Symlink the panel into Glyphs 3's Scripts folder.")
    panel_install.set_defaults(func=cmd_panel_install)

    return proof


def dispatch(args) -> int:
    """Invoke whichever subcommand handler `parse_args` stashed on `args.func`."""
    func = getattr(args, "func", None)
    if func is None:
        print("FAIL: no proof subcommand given. Try `glyph-audit proof --help`.",
              file=sys.stderr)
        return 2
    return func(args)
