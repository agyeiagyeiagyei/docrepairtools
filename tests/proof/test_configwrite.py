"""Unit tests for the panel's config writer (pin-to-master).

`configwrite.py` is deliberately vanilla-free so these run outside
Glyphs.app. The writer's contract: surgical edits that never disturb
comments or unrelated sections.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from GlyphAudit.proof.panel.configwrite import (
    CONFIG_TEMPLATE,
    load_audit_references,
    pin_reference_for_master,
)


@pytest.fixture
def config(tmp_path):
    return tmp_path / "config.toml"


class TestPinNewFile:
    def test_creates_file_from_template(self, config):
        pin_reference_for_master("Italic", "/fonts/Ref-Italic.ttf", config)
        text = config.read_text()
        # Template's comment header survives...
        assert "GlyphAudit config" in text
        # ...and the new section landed.
        assert "[instances.Italic]" in text
        assert 'ref = "/fonts/Ref-Italic.ttf"' in text

    def test_roundtrips_through_reader(self, config):
        pin_reference_for_master("Italic", "/fonts/Ref-Italic.ttf", config)
        refs = load_audit_references(config)
        assert refs == {"italic": "/fonts/Ref-Italic.ttf"}


class TestPinExistingFile:
    BASE = """\
# my precious comments
[defaults]
filter = "yellow"

[instances.Regular]
ref = "/fonts/Reg.ttf"

[instances.Bold]
ref = "/fonts/Bold.ttf"

# trailing note
"""

    def test_appends_new_master(self, config):
        config.write_text(self.BASE)
        pin_reference_for_master("Italic", "/fonts/Ital.ttf", config)
        refs = load_audit_references(config)
        assert refs["italic"] == "/fonts/Ital.ttf"
        # Untouched sections survive byte-for-byte concerns: comments intact.
        text = config.read_text()
        assert "# my precious comments" in text
        assert "# trailing note" in text
        assert 'ref = "/fonts/Reg.ttf"' in text

    def test_replaces_existing_master(self, config):
        config.write_text(self.BASE)
        pin_reference_for_master("Bold", "/fonts/NewBold.ttf", config)
        refs = load_audit_references(config)
        assert refs["bold"] == "/fonts/NewBold.ttf"
        text = config.read_text()
        assert "/fonts/Bold.ttf" not in text          # old ref replaced
        assert 'ref = "/fonts/Reg.ttf"' in text        # sibling untouched
        assert text.count("[instances.Bold]") == 1     # no duplicate section

    def test_case_insensitive_replacement(self, config):
        # Reader lowercases keys, so `bold` and `Bold` are the same entry —
        # pinning `bold` must update `[instances.Bold]`, not append a
        # second section that shadows it.
        config.write_text(self.BASE)
        pin_reference_for_master("bold", "/fonts/NewBold.ttf", config)
        text = config.read_text()
        assert text.lower().count("[instances.bold]") == 1
        assert load_audit_references(config)["bold"] == "/fonts/NewBold.ttf"

    def test_multiword_master_quoted_header(self, config):
        config.write_text(self.BASE)
        pin_reference_for_master("Bold Italic", "/fonts/BI.ttf", config)
        text = config.read_text()
        # TOML requires quoting for keys with spaces.
        assert '[instances."Bold Italic"]' in text
        assert load_audit_references(config)["bold italic"] == "/fonts/BI.ttf"

    def test_multiword_master_replaces_existing_quoted(self, config):
        config.write_text(self.BASE + '\n[instances."Bold Italic"]\nref = "/old/BI.ttf"\n')
        pin_reference_for_master("Bold Italic", "/new/BI.ttf", config)
        refs = load_audit_references(config)
        assert refs["bold italic"] == "/new/BI.ttf"
        assert config.read_text().count('[instances."Bold Italic"]') == 1

    def test_path_with_spaces(self, config):
        config.write_text(self.BASE)
        pin_reference_for_master(
            "Italic", "/Users/me/My Fonts/Ref Italic.ttf", config,
        )
        assert load_audit_references(config)["italic"] == "/Users/me/My Fonts/Ref Italic.ttf"

    def test_pin_all_four_masters(self, config):
        # The actual workflow this exists for: map every master once,
        # then never touch the dropdown again.
        for master, path in [
            ("Regular", "/r/VERDANA.TTF"),
            ("Bold", "/r/VERDANAB.TTF"),
            ("Italic", "/r/VERDANAI.TTF"),
            ("Bold Italic", "/r/VERDANABI.TTF"),
        ]:
            pin_reference_for_master(master, path, config)
        refs = load_audit_references(config)
        assert refs == {
            "regular": "/r/VERDANA.TTF",
            "bold": "/r/VERDANAB.TTF",
            "italic": "/r/VERDANAI.TTF",
            "bold italic": "/r/VERDANABI.TTF",
        }


class TestEdgeCases:
    def test_empty_master_is_noop(self, config):
        pin_reference_for_master("", "/fonts/X.ttf", config)
        assert not config.exists()

    def test_empty_path_is_noop(self, config):
        pin_reference_for_master("Italic", "", config)
        assert not config.exists()

    def test_repin_same_master_is_idempotent(self, config):
        pin_reference_for_master("Italic", "/fonts/A.ttf", config)
        first = config.read_text()
        pin_reference_for_master("Italic", "/fonts/A.ttf", config)
        assert config.read_text() == first

    def test_section_without_ref_line_gets_one(self, config):
        # A hand-edited section that has axis but no ref — insert, don't dup.
        config.write_text("[instances.Italic]\naxis = { wght = 400 }\n")
        pin_reference_for_master("Italic", "/fonts/I.ttf", config)
        refs = load_audit_references(config)
        assert refs["italic"] == "/fonts/I.ttf"
        assert config.read_text().count("[instances.Italic]") == 1
