"""L1 tests for the CLI dispatcher.

Focused on the subcommand routing surface — the individual command
handlers are exercised indirectly by the manual smoke checklist since
they invoke fontc / spawn servers.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from GlyphAudit.cli import main


class TestBackcompatRouting:
    """The legacy `glyph-audit --target …` form must still work — pre-0.2
    scripts (Velarium's Makefile, other users' CI) rely on it.
    """

    def test_top_level_flag_routes_to_audit(self, monkeypatch, tmp_path, capsys):
        # We don't want to actually run the audit (needs a real font).
        # Just verify parsing succeeds by asserting the `_run` callable is
        # what gets invoked.
        called = {}

        def fake_run_audit(args, parser):
            called["target"] = args.target
            return 0

        # Patch the runner AND the parser's `set_defaults` binding.
        monkeypatch.setattr("GlyphAudit.cli._run_audit", fake_run_audit)
        rc = main(["--target", "/nonexistent.glyphspackage",
                   "--pair", "Regular=/nonexistent-ref.ttf"])
        assert rc == 0
        assert called["target"] == "/nonexistent.glyphspackage"

    def test_explicit_audit_subcommand(self, monkeypatch):
        # Same behaviour without the legacy shim path.
        called = {}
        monkeypatch.setattr("GlyphAudit.cli._run_audit",
                            lambda args, parser: (called.setdefault("t", args.target), 0)[1])
        rc = main(["audit", "--target", "/x.glyphspackage",
                   "--pair", "Regular=/y.ttf"])
        assert rc == 0
        assert called["t"] == "/x.glyphspackage"


class TestProofRouting:
    def test_help_exits_cleanly(self):
        with pytest.raises(SystemExit) as exc:
            main(["proof", "-h"])
        assert exc.value.code == 0

    def test_missing_subcommand_exits(self):
        # argparse's `required=True` triggers a usage error → SystemExit 2.
        with pytest.raises(SystemExit) as exc:
            main(["proof"])
        assert exc.value.code == 2

    def test_build_requires_config(self, tmp_path, monkeypatch, capsys):
        # No glyph-audit.toml anywhere — should fail loudly. Exit code 2
        # arrives via `sys.exit()` inside `_resolve_config`, hence SystemExit.
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as exc:
            main(["proof", "build"])
        assert exc.value.code == 2
        assert "no glyph-audit.toml found" in capsys.readouterr().err

    def test_build_with_config(self, tmp_path, monkeypatch, capsys):
        # Real config → real dispatch. build_font will fail because the
        # source doesn't exist, but the CLI should route + return
        # non-zero (not raise).
        cfg_path = tmp_path / "glyph-audit.toml"
        cfg_path.write_text('''
[proof]
family_name = "Test"
sources = ["nonexistent.glyphspackage"]
output_dir = "out"
''')
        monkeypatch.chdir(tmp_path)
        rc = main(["proof", "build"])
        assert rc == 1  # source missing → build_font returns False


class TestSubcommandStructure:
    """Anchor tests: guard against accidentally dropping a subcommand
    or renaming one out from under downstream users.
    """

    def test_all_proof_subcommands_present(self, capsys):
        with pytest.raises(SystemExit):
            main(["proof", "-h"])
        help_text = capsys.readouterr().out
        for sub in ("build", "watch", "serve", "panel"):
            assert sub in help_text, f"proof subcommand `{sub}` missing from help"

    def test_top_level_lists_subcommands(self, capsys):
        with pytest.raises(SystemExit):
            main(["-h"])
        help_text = capsys.readouterr().out
        assert "audit" in help_text
        assert "proof" in help_text
