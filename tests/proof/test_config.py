"""Unit tests for GlyphAudit.proof.config — validation + discovery.

These stay cheap (no fontc, no filesystem beyond tmp_path). They fire on
every commit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from GlyphAudit.proof.config import (
    ConfigError,
    DEFAULT_PROOF_COLORS,
    GLYPHS_COLORS,
    ProjectConfig,
    Reference,
    load_project_config,
    normalize_color,
    validate_colors,
)


# ---------------------------------------------------------------------------
# normalize_color / validate_colors
# ---------------------------------------------------------------------------

class TestNormalizeColor:
    def test_int_becomes_str(self):
        assert normalize_color(3) == "3"

    def test_str_lowercased_and_stripped(self):
        assert normalize_color("  NONE ") == "none"

    def test_str_preserved(self):
        assert normalize_color("4") == "4"

    def test_bool_rejected(self):
        # bool is an int subclass in Python — the guard has to be explicit
        # or `true`/`false` in TOML silently becomes color "1"/"0", which
        # would filter WAY more glyphs than the user intended.
        with pytest.raises(ConfigError, match="bool"):
            normalize_color(True)
        with pytest.raises(ConfigError, match="bool"):
            normalize_color(False)

    def test_none_python_rejected(self):
        with pytest.raises(ConfigError):
            normalize_color(None)


class TestValidateColors:
    def test_accepts_ints(self):
        assert validate_colors([3, 4]) == frozenset({"3", "4"})

    def test_accepts_strings(self):
        assert validate_colors(["3", "4", "none"]) == frozenset({"3", "4", "none"})

    def test_accepts_mixed_types(self):
        assert validate_colors([3, "4", "none"]) == frozenset({"3", "4", "none"})

    def test_rejects_unknown_index(self):
        with pytest.raises(ConfigError, match="invalid color"):
            validate_colors([12])           # only 0..11 defined
        with pytest.raises(ConfigError, match="invalid color"):
            validate_colors([-1])

    def test_rejects_unknown_string(self):
        with pytest.raises(ConfigError, match="invalid color"):
            validate_colors(["orange"])     # human label, not a key

    def test_empty_returns_empty(self):
        # Callers decide what to do with an empty set. `[proof].colors` in
        # config gets an early "must include at least one" check.
        assert validate_colors([]) == frozenset()

    def test_default_matches_yellow_lightgreen(self):
        # Anchor test: catches an accidental drift of the default subset.
        assert DEFAULT_PROOF_COLORS == frozenset({"3", "4"})

    def test_palette_size(self):
        assert len(GLYPHS_COLORS) == 13     # 12 colours + "none"


# ---------------------------------------------------------------------------
# load_project_config: discovery + parsing
# ---------------------------------------------------------------------------

VALID_TOML = """
[project]
name = "TestFont"

[proof]
family_name = "TestFont Proof"
sources = ["TestFont.glyphspackage"]
colors  = [3, 4]

[references.Verdana]
regular = "sources/reference/Verdana.ttf"
"""


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


class TestDiscovery:
    def test_finds_config_in_cwd(self, tmp_path):
        _write(tmp_path / "glyph-audit.toml", VALID_TOML)
        cfg = load_project_config(tmp_path)
        assert cfg is not None
        assert cfg.name == "TestFont"
        assert cfg.proof.family_name == "TestFont Proof"

    def test_finds_config_walking_up(self, tmp_path):
        # Two levels of nesting shouldn't matter — the tool typically runs
        # from `sources/` or `proof-app/` relative to the config file.
        deep = tmp_path / "sub" / "deeper"
        deep.mkdir(parents=True)
        _write(tmp_path / "glyph-audit.toml", VALID_TOML)
        cfg = load_project_config(deep)
        assert cfg is not None
        assert cfg.project_root == tmp_path.resolve()

    def test_prefers_dotless_name(self, tmp_path):
        # `.glyph-audit.toml` is the dotfile alternative for tidy checkouts;
        # the canonical `glyph-audit.toml` should win when both exist.
        _write(tmp_path / ".glyph-audit.toml", VALID_TOML.replace('"TestFont"', '"DotVersion"'))
        _write(tmp_path / "glyph-audit.toml",  VALID_TOML)
        cfg = load_project_config(tmp_path)
        assert cfg is not None
        assert cfg.name == "TestFont"

    def test_missing_returns_none(self, tmp_path):
        # Callers treat None as "no project config — either supply defaults
        # or fail loudly with your own error message".
        assert load_project_config(tmp_path) is None


class TestValidation:
    def test_missing_proof_section(self, tmp_path):
        _write(tmp_path / "glyph-audit.toml", '[project]\nname = "X"\n')
        with pytest.raises(ConfigError, match="\\[proof\\]"):
            load_project_config(tmp_path)

    def test_missing_family_name(self, tmp_path):
        _write(tmp_path / "glyph-audit.toml", '''
[proof]
sources = ["X.glyphspackage"]
''')
        with pytest.raises(ConfigError, match="family_name"):
            load_project_config(tmp_path)

    def test_empty_sources(self, tmp_path):
        _write(tmp_path / "glyph-audit.toml", '''
[proof]
family_name = "X"
sources = []
''')
        with pytest.raises(ConfigError, match="at least one source"):
            load_project_config(tmp_path)

    def test_empty_colors(self, tmp_path):
        _write(tmp_path / "glyph-audit.toml", '''
[proof]
family_name = "X"
sources = ["X.glyphspackage"]
colors  = []
''')
        with pytest.raises(ConfigError, match="at least one color"):
            load_project_config(tmp_path)

    def test_invalid_color_key(self, tmp_path):
        _write(tmp_path / "glyph-audit.toml", '''
[proof]
family_name = "X"
sources = ["X.glyphspackage"]
colors  = [3, 99]
''')
        with pytest.raises(ConfigError, match="invalid color"):
            load_project_config(tmp_path)

    def test_default_colors_when_omitted(self, tmp_path):
        _write(tmp_path / "glyph-audit.toml", '''
[proof]
family_name = "X"
sources = ["X.glyphspackage"]
''')
        cfg = load_project_config(tmp_path)
        assert cfg.proof.colors == DEFAULT_PROOF_COLORS

    def test_output_basename_derived_from_family(self, tmp_path):
        _write(tmp_path / "glyph-audit.toml", '''
[proof]
family_name = "My Cool Family"
sources = ["X.glyphspackage"]
''')
        cfg = load_project_config(tmp_path)
        assert cfg.proof.output_basename == "my-cool-family"


class TestReferences:
    def test_relative_paths_resolved_against_project_root(self, tmp_path):
        _write(tmp_path / "glyph-audit.toml", '''
[proof]
family_name = "X"
sources = ["X.glyphspackage"]

[references.Verdana]
regular = "sources/reference/Verdana.ttf"
''')
        cfg = load_project_config(tmp_path)
        assert len(cfg.proof.references) == 1
        ref = cfg.proof.references[0]
        assert ref.name == "Verdana"
        assert len(ref.slots) == 1
        # Path resolved relative to project root — not to CWD, not left raw.
        assert ref.slots[0].path == str((tmp_path / "sources/reference/Verdana.ttf").resolve())

    def test_home_expansion(self, tmp_path):
        _write(tmp_path / "glyph-audit.toml", '''
[proof]
family_name = "X"
sources = ["X.glyphspackage"]

[references.Home]
regular = "~/fonts/Home.ttf"
''')
        cfg = load_project_config(tmp_path)
        ref = cfg.proof.references[0]
        # `~` should be expanded to the user home dir — otherwise a
        # user-friendly path in config becomes a literal '~' file lookup.
        assert not ref.slots[0].path.startswith("~")
        assert "fonts/Home.ttf" in ref.slots[0].path

    def test_absolute_paths_preserved(self, tmp_path):
        _write(tmp_path / "glyph-audit.toml", '''
[proof]
family_name = "X"
sources = ["X.glyphspackage"]

[references.Abs]
regular = "/etc/fonts/Abs.ttf"
''')
        cfg = load_project_config(tmp_path)
        assert cfg.proof.references[0].slots[0].path == "/etc/fonts/Abs.ttf"

    def test_unknown_slot_key_silently_skipped(self, tmp_path):
        # Forward-compat: future slot names (e.g., `condensed_bold`) shouldn't
        # break older tool versions that haven't learned them yet.
        _write(tmp_path / "glyph-audit.toml", '''
[proof]
family_name = "X"
sources = ["X.glyphspackage"]

[references.Odd]
regular       = "/etc/reg.ttf"
condensed_ext = "/etc/cext.ttf"
''')
        cfg = load_project_config(tmp_path)
        slots = cfg.proof.references[0].slots
        assert [s.slot for s in slots] == ["regular"]

    def test_empty_reference_errors(self, tmp_path):
        _write(tmp_path / "glyph-audit.toml", '''
[proof]
family_name = "X"
sources = ["X.glyphspackage"]

[references.Empty]
whatever = "/nowhere.ttf"
''')
        with pytest.raises(ConfigError, match="no known slot keys"):
            load_project_config(tmp_path)

    def test_has_italic_helper(self, tmp_path):
        _write(tmp_path / "glyph-audit.toml", '''
[proof]
family_name = "X"
sources = ["X.glyphspackage"]

[references.WithItalic]
regular = "/a.ttf"
italic  = "/b.ttf"

[references.NoItalic]
regular = "/c.ttf"
''')
        cfg = load_project_config(tmp_path)
        refs = {r.name: r for r in cfg.proof.references}
        assert refs["WithItalic"].has_italic is True
        assert refs["NoItalic"].has_italic is False


class TestItalicDetection:
    def test_roman_and_italic_split(self, tmp_path):
        _write(tmp_path / "glyph-audit.toml", '''
[proof]
family_name = "X"
sources = [
  "X.glyphspackage",
  "X Italic.glyphspackage",
  "X-italic-Bold.glyphspackage",
]
''')
        cfg = load_project_config(tmp_path)
        assert cfg.proof.roman_sources() == ("X.glyphspackage",)
        assert set(cfg.proof.italic_sources()) == {
            "X Italic.glyphspackage",
            "X-italic-Bold.glyphspackage",
        }
