#MenuTitle: Glyph Proof
# -*- coding: utf-8 -*-
"""Glyphs.app panel for the `glyph-audit proof` browser view.

Companion to `Width Audit.py` — the two used to share one window; splitting
them lets each be sized for its own concern (see the split's design notes
in the repo README). This one:

  - Color-flag filter for the proof subset (all 12 Glyphs colors + no-color).
  - Launch / stop `glyph-audit proof serve --watch` — file-watcher rebuilds
    the proof TTFs as you edit outlines / features / kerning, packaged web
    app serves the compare view at http://localhost:5173.
  - Live subprocess log tail + status heuristic.

Toggle: run the menu item again to close. Killing the panel stops the
subprocess cleanly.

Install
-------
    pip install docrepair-glyph-audit
    glyph-audit proof panel install
"""

import os
import shlex
import signal
import subprocess
import sys
import threading
import traceback
import webbrowser
from pathlib import Path

# sys.path bootstrap — inline because Glyphs.app runs a symlinked script
# as a standalone module (no `__package__`), so `from ._foo import …`
# blows up with ImportError. Walk up until we hit a directory containing
# `GlyphAudit/__init__.py` and prepend it. No-op when GlyphAudit is
# already importable (pip-install case).
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
# full rationale (sys.modules survives "Reload Scripts", so stale
# common.py otherwise keeps loading forever).
for _mod in [m for m in sys.modules if m == "GlyphAudit" or m.startswith("GlyphAudit.")]:
    del sys.modules[_mod]

import vanilla
from GlyphsApp import Glyphs

from GlyphAudit.proof.config import DEFAULT_PROOF_COLORS, GLYPHS_COLORS
from GlyphAudit.proof.panel.common import (
    DEV_SERVER_URL,
    PROOF_STATE_PATH,
    find_glyph_audit_cli,
    load_state,
    login_shell_path,
    project_config_for,
    save_state,
)


# ---------------------------------------------------------------------------
# Proof pipeline subprocess
# ---------------------------------------------------------------------------

class ProofPipeline:
    """Manage the `glyph-audit proof serve --watch` subprocess.

    Output flow: a background Python thread does blocking readline() on
    the child's stdout pipe and hands each line to a main-thread callback
    via `PyObjCTools.AppHelper.callAfter`. This sidesteps NSTimer + the
    Python-vs-NSObject selector-dispatch trap that broke the earlier
    polling-timer approach.
    """

    def __init__(self, project_root, colors, sources):
        self.project_root = Path(project_root)
        self.colors = frozenset(colors)
        self.sources = list(sources)
        self._proc = None
        self._reader_thread = None

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, on_log) -> None:
        env = os.environ.copy()
        env["NO_COLOR"] = "1"
        env["FORCE_COLOR"] = "0"
        # Force line-buffered stdout in the CHILD's Python; Popen's bufsize
        # only affects the parent's read side.
        env["PYTHONUNBUFFERED"] = "1"
        # Give the subprocess the user's Terminal PATH so downstream CLIs
        # (fontc especially) are findable. Glyphs.app inherits launchd's
        # stripped PATH otherwise.
        shell_path = login_shell_path()
        if shell_path:
            existing = env.get("PATH", "")
            merged, seen = [], set()
            for part in shell_path.split(":") + existing.split(":"):
                if part and part not in seen:
                    merged.append(part)
                    seen.add(part)
            env["PATH"] = ":".join(merged)

        cmd = find_glyph_audit_cli() + [
            "proof", "serve",
            "--watch",
            "--no-browser",
            "--colors", ",".join(sorted(self.colors)),
        ]
        for s in self.sources:
            cmd += ["--source", s]

        on_log(f"$ (cd {self.project_root}) {shlex.join(cmd)}")
        self._proc = subprocess.Popen(
            cmd,
            cwd=str(self.project_root),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=env,
        )
        # Quick smoke test: catch immediate death.
        import time as _t
        _t.sleep(0.25)
        if self._proc.poll() is not None:
            try:
                remnant = self._proc.stdout.read() if self._proc.stdout else ""
            except Exception:
                remnant = ""
            for line in (remnant or "").splitlines():
                if line.strip():
                    on_log(line)
            on_log(f"[proof] subprocess exited immediately (code {self._proc.returncode})")
            self._proc = None
            return

        # Reader thread.
        proc = self._proc
        try:
            from PyObjCTools import AppHelper

            def _dispatch(line):
                try:
                    on_log(line)
                except Exception:
                    pass

            def _run():
                try:
                    for raw in iter(proc.stdout.readline, ""):
                        line = raw.rstrip("\n")
                        if not line:
                            continue
                        AppHelper.callAfter(_dispatch, line)
                    AppHelper.callAfter(_dispatch,
                        f"[proof] pipe closed (exit code {proc.poll()})")
                except Exception as e:
                    AppHelper.callAfter(_dispatch, f"[proof] reader crashed: {e}")

            self._reader_thread = threading.Thread(
                target=_run, name="glyph-audit-proof-reader", daemon=True,
            )
            self._reader_thread.start()
        except ImportError:
            on_log("[proof] PyObjCTools not available — output streaming disabled")

    def stop(self, on_log=None) -> None:
        p = self._proc
        if p is None:
            return
        try:
            p.send_signal(signal.SIGTERM)
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                p.kill()
            if on_log:
                on_log("[proof] stopped")
        except Exception:
            if on_log:
                on_log(f"[proof] stop failed:\n{traceback.format_exc()}")
        self._proc = None


# ---------------------------------------------------------------------------
# The panel
# ---------------------------------------------------------------------------

# Registry on `builtins` rather than a class attribute — the module purge
# above gives every menu-item run a fresh class, so `cls._instance` would
# always be None and toggle-to-close would stack windows instead. See
# audit_panel.py for the same pattern.
import builtins as _builtins


def _panel_registry() -> dict:
    reg = getattr(_builtins, "_glyphaudit_panel_registry", None)
    if reg is None:
        reg = {}
        _builtins._glyphaudit_panel_registry = reg
    return reg


class GlyphProofPanel:
    REGISTRY_KEY = "glyph_proof"

    @classmethod
    def toggle(cls) -> None:
        reg = _panel_registry()
        existing = reg.get(cls.REGISTRY_KEY)
        if existing is not None:
            # Pre-purge instance — bound methods still work; closing also
            # stops any running proof subprocess.
            try:
                existing.close()
            except Exception:
                traceback.print_exc()
            reg[cls.REGISTRY_KEY] = None
            return
        if Glyphs.font is None:
            Glyphs.showMacroWindow()
            print("Glyph Proof: open a font first.")
            return
        reg[cls.REGISTRY_KEY] = cls()

    def __init__(self) -> None:
        self.font = Glyphs.font
        self.project_cfg, self.project_reason = project_config_for(self.font)
        self._pipeline = None
        self._log_tail = []
        self._log_max = 40

        state = load_state(PROOF_STATE_PATH)
        saved_colors = state.get("proof_colors")
        if isinstance(saved_colors, list):
            self._colors = frozenset(str(c) for c in saved_colors)
        elif self.project_cfg:
            self._colors = self.project_cfg.proof.colors
        else:
            self._colors = DEFAULT_PROOF_COLORS

        self._sources = list(self.project_cfg.proof.sources) if self.project_cfg else []

        self._build_ui()
        self.w.open()

    # ----- UI ------------------------------------------------------------

    def _build_ui(self) -> None:
        # Compact by default — the log is useful when a build is running
        # but doesn't need to dominate the panel between builds. Users
        # can drag it taller if they want to see more scrollback.
        W, H = 660, 340
        self.w = vanilla.FloatingWindow(
            (W, H), "Glyph Proof",
            autosaveName="GlyphProofPanel", minSize=(560, 280),
        )
        y = 12

        # Header — family + subtitle that names the scope explicitly.
        family = self.font.familyName if self.font else "(no font)"
        self.w.headerTitle = vanilla.TextBox((10, y, 400, 20), family, sizeStyle="regular")
        y += 22
        self.w.headerSub = vanilla.TextBox(
            (10, y, -10, 14),
            "compiles every ticked source into a TTF the browser can render — "
            "independent of what's open in Glyphs",
            sizeStyle="mini",
        )
        y += 24

        # Compile row — one checkbox per configured source.
        self.w.sourcesLabel = vanilla.TextBox((10, y, 80, 18), "Compile:", sizeStyle="small")
        self._source_checkboxes = []
        sources_for_ui = self._sources or ["(no sources — glyph-audit.toml missing)"]
        for i, src in enumerate(sources_for_ui[:2]):
            cb = vanilla.CheckBox(
                (90 + i * 260, y - 2, 250, 22),
                src,
                value=src in self._sources,
                callback=self._sources_changed_cb,
            )
            cb.enable(self.project_cfg is not None)
            setattr(self.w, f"sourceCb{i}", cb)
            self._source_checkboxes.append((src, cb))
        y += 30

        # Colours — 13 checkboxes (2 rows × 7). Cell width sized to fit
        # inside the 660-wide window: label anchor at 60 + 7 cells × 80
        # = 620, leaving a 30px right margin. Previously at col_w=90 the
        # last cell of row 0 (`Lt blue`) was clipped by the window edge.
        self.w.colorsLabel = vanilla.TextBox((10, y, 60, 18), "Colors:", sizeStyle="small")
        self._color_checkboxes = []
        col_w, col_h, per_row = 80, 20, 7
        for idx, (key, label) in enumerate(GLYPHS_COLORS):
            row, col = idx // per_row, idx % per_row
            cb = vanilla.CheckBox(
                (60 + col * col_w, y - 2 + row * col_h, col_w - 4, col_h),
                label,
                value=key in self._colors,
                callback=self._colors_changed_cb,
            )
            cb.enable(self.project_cfg is not None)
            setattr(self.w, f"colorCb{key.replace('none','X')}", cb)
            self._color_checkboxes.append((key, cb))
        y += col_h * 2 + 8

        # Launch controls
        self.w.launchBtn = vanilla.Button(
            (10, y, 180, 22), "Launch proof window",
            callback=self._launch_clicked_cb,
        )
        self.w.openBrowserBtn = vanilla.Button(
            (200, y, 100, 22), "Open browser",
            callback=lambda sender: webbrowser.open(DEV_SERVER_URL),
        )
        self.w.openBrowserBtn.enable(False)
        default_status = (
            "idle" if self.project_cfg else
            f"no config — {self.project_reason}"
        )
        self.w.pipelineStatus = vanilla.TextBox(
            (310, y + 2, -10, 20), default_status, sizeStyle="small",
        )
        y += 30

        self.w.log = vanilla.TextEditor((10, y, -10, -10), "", readOnly=True)

        self.w.bind("close", self._on_close)

    # ----- lifecycle ------------------------------------------------------

    def close(self) -> None:
        try:
            if self._pipeline and self._pipeline.is_running():
                self._pipeline.stop()
            self.w.close()
        finally:
            _panel_registry()[self.REGISTRY_KEY] = None

    def _on_close(self, sender) -> None:
        _panel_registry()[self.REGISTRY_KEY] = None
        if self._pipeline and self._pipeline.is_running():
            self._pipeline.stop()

    # ----- launch / stop -------------------------------------------------

    def _launch_clicked_cb(self, sender) -> None:
        if self._pipeline and self._pipeline.is_running():
            self._pipeline.stop(self._append_log)
            self._reflect_pipeline_state()
            return
        if self.project_cfg is None:
            self._append_log(f"Can't launch: {self.project_reason}")
            return
        active_sources = [s for s, cb in self._source_checkboxes if cb.get()]
        if not active_sources:
            self._append_log("Can't launch: at least one source must be checked.")
            return
        if not self._colors:
            self._append_log("Can't launch: at least one color must be selected.")
            return
        self._pipeline = ProofPipeline(
            self.project_cfg.project_root,
            self._colors,
            active_sources,
        )
        try:
            self._pipeline.start(self._append_log)
        except (FileNotFoundError, RuntimeError) as e:
            self._append_log(f"Launch failed — {e}")
            self._pipeline = None
        except Exception:
            self._append_log(traceback.format_exc())
            self._pipeline = None
        self._reflect_pipeline_state()

    def _reflect_pipeline_state(self) -> None:
        running = self._pipeline is not None and self._pipeline.is_running()
        self.w.launchBtn.setTitle("Stop proof window" if running else "Launch proof window")
        self.w.openBrowserBtn.enable(running)
        if running:
            current = str(self.w.pipelineStatus.get() or "")
            if current in ("idle", "", self.project_reason or ""):
                self.w.pipelineStatus.set("starting…")
        else:
            self.w.pipelineStatus.set("idle" if self.project_cfg else self.project_reason)

    def _append_log(self, line: str) -> None:
        self._log_tail.append(line.rstrip())
        if len(self._log_tail) > self._log_max:
            self._log_tail = self._log_tail[-self._log_max:]
        try:
            self.w.log.set("\n".join(self._log_tail))
        except Exception:
            pass
        try:
            self._update_status_from_log()
        except Exception:
            pass
        if self._pipeline is not None and (
            "pipe closed" in line or "subprocess exited immediately" in line
        ):
            try:
                self._pipeline._proc = None
            except Exception:
                pass
            self._reflect_pipeline_state()

    def _update_status_from_log(self) -> None:
        tail = " ".join(self._log_tail[-6:])
        if "FAIL" in tail or "fontc failed" in tail:
            self.w.pipelineStatus.set("build failed — see log")
        elif "pipe closed" in tail or "exited" in tail:
            self.w.pipelineStatus.set("stopped")
        elif "Serving proof app" in tail:
            self.w.pipelineStatus.set("serving")
        elif "Built:" in tail:
            self.w.pipelineStatus.set("up to date")
        elif "change detected" in tail or "Compiling" in tail:
            self.w.pipelineStatus.set("building…")

    # ----- sources / colors ---------------------------------------------

    def _sources_changed_cb(self, sender) -> None:
        # Source checkboxes are informational for now — the launch cmd
        # reads them at click time. Not persisted separately; the config
        # is the canonical source list.
        pass

    def _colors_changed_cb(self, sender) -> None:
        self._colors = frozenset(k for k, cb in self._color_checkboxes if cb.get())
        state = load_state(PROOF_STATE_PATH)
        state["proof_colors"] = sorted(self._colors)
        save_state(PROOF_STATE_PATH, state)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

try:
    GlyphProofPanel.toggle()
except Exception:
    Glyphs.showMacroWindow()
    print("Glyph Proof: failed to launch.")
    print(traceback.format_exc())
