"""L2 integration tests for GlyphAudit.proof.build.build_font.

These require fontc on the PATH — skipped automatically when it's not
installed. `pip install glyph-audit[proof]` pulls fontc in.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(
    shutil.which("fontc") is None,
    reason="fontc not on PATH — install via `pip install glyph-audit[proof]`",
)

# Only import fontTools if it's here; the whole file gets skipped if fontc
# is missing, but the parity checks below need fontTools too.
fontTools = pytest.importorskip("fontTools")
from fontTools.ttLib import TTFont

from GlyphAudit.proof.build import build_font


# ---------------------------------------------------------------------------
# Fixture with an intentionally-broken frac feature — used to test the
# feature filter. `frac` substitutes to `.dnom` glyphs that DON'T exist in
# the source, which would normally cause fontc to reject the whole feature
# file. The filter should strip those bracket-list rules before fontc runs.
# ---------------------------------------------------------------------------

_BROKEN_FRAC_FEATURES = """features = (
{
automatic = 1;
code = "lookup FRAC {
\tsub slash by fraction;
} FRAC;
lookup UP {
\tsub [zero one two] by [zero.numr one.numr two.numr];
} UP;
";
tag = frac;
}
);
"""


class TestBuildEndToEnd:
    def test_defaults_yellow_lightgreen(self, tiny_source, tmp_path):
        out = tmp_path / "out"
        ok = build_font(
            source_path=str(tiny_source),
            output_dir=str(out),
            output_basename="tiny",
        )
        assert ok, "build_font returned False"
        ttf = out / "tiny.ttf"
        assert ttf.exists(), "TTF not produced"

        f = TTFont(ttf)
        # Yellow + lightgreen from the default fixture:
        #   a (yellow) → /a
        #   b (lightgreen) → /b
        #   A (yellow, has component ref) → /A
        # Plus essentials that always survive.
        cmap = f.getBestCmap()
        assert 0x61 in cmap and cmap[0x61] == "a"
        assert 0x62 in cmap and cmap[0x62] == "b"
        assert 0x41 in cmap and cmap[0x41] == "A"

        # `c` is red (colour 0), `d` uncoloured — both filtered out.
        assert 0x63 not in cmap, "/c leaked into the subset"
        assert 0x64 not in cmap, "/d leaked into the subset"

    def test_all_colours_keeps_c_and_d(self, tiny_source, tmp_path):
        out = tmp_path / "out"
        build_font(
            source_path=str(tiny_source),
            output_dir=str(out),
            output_basename="tiny",
            proof_colors={"0", "3", "4", "none"},
        )
        cmap = TTFont(out / "tiny.ttf").getBestCmap()
        assert 0x63 in cmap, "/c should be included when colour 0 is selected"
        assert 0x64 in cmap, "/d should be included when 'none' is selected"

    def test_transitive_component_closure(self, tiny_source, tmp_path):
        # /A references /acomb (uncoloured). Even at the default colour set
        # (yellow+lightgreen only), /acomb must land in the TTF or fontc
        # panics trying to resolve the composite.
        out = tmp_path / "out"
        ok = build_font(
            source_path=str(tiny_source),
            output_dir=str(out),
            output_basename="tiny",
        )
        assert ok, (
            "build_font returned False — likely fontc rejected the composite "
            "because component closure didn't include /acomb"
        )
        # fontc may rename combining marks to their `uniXXXX` AGL form
        # (`acomb` → `uni0363` — Combining Latin Small Letter A), so we
        # count glyphs rather than search by exact source name. The essential
        # test is that the composite /A compiled (build returned True) AND
        # the total glyph count reflects the pulled-in dependency.
        f = TTFont(out / "tiny.ttf")
        order = f.getGlyphOrder()
        # 5 kept (essentials + a/b/A) + 1 closure (acomb, possibly renamed)
        # + fontc's implicit .notdef = 7 minimum
        assert len(order) >= 7, (
            f"component closure failed: expected ≥7 glyphs, got {len(order)}: {order}"
        )

    def test_essential_glyphs_survive_narrow_filter(self, tiny_source, tmp_path):
        # No colour matches anything in the fixture (nothing is colour 5).
        # But _notdef + space must still be kept — else the TTF is invalid.
        out = tmp_path / "out"
        ok = build_font(
            source_path=str(tiny_source),
            output_dir=str(out),
            output_basename="tiny",
            proof_colors={"5"},
        )
        assert ok
        f = TTFont(out / "tiny.ttf")
        order = f.getGlyphOrder()
        assert ".notdef" in order or "_notdef" in order
        # space glyph must be present so cmap lookup for U+0020 resolves.
        assert 0x20 in f.getBestCmap()

    def test_feature_filter_strips_broken_frac(self, tiny_source_factory, tmp_path):
        # Fixture with a frac feature substituting to nonexistent numr/dnom
        # glyphs. fontc would reject the whole feature file if the filter
        # let those rules through — the build should succeed regardless.
        src = tiny_source_factory(
            name="WithFrac.glyphspackage",
            features_block=_BROKEN_FRAC_FEATURES,
        )
        out = tmp_path / "out"
        ok = build_font(
            source_path=str(src),
            output_dir=str(out),
            output_basename="wf",
        )
        assert ok, (
            "build_font failed — the broken frac feature wasn't stripped"
        )

    def test_manifests_written(self, tiny_source, tmp_path):
        out = tmp_path / "out"
        build_font(
            source_path=str(tiny_source),
            output_dir=str(out),
            output_basename="tiny",
        )
        chars = json.loads((out / "available-chars.json").read_text())
        features = json.loads((out / "available-features.json").read_text())
        # `chars` is a list of codepoints — `a`, `b`, `A`, `space`.
        assert 0x61 in chars
        # `features` is a list of {tag, name, status, …}. Our fixture
        # has no features → list may be empty, must at least be a list.
        assert isinstance(features, list)

    def test_italic_source_output_paths(self, tiny_source_factory, tmp_path):
        src = tiny_source_factory(
            name="Tiny Italic.glyphspackage",
            family_name="Tiny",
        )
        out = tmp_path / "out"
        ok = build_font(
            source_path=str(src),
            output_dir=str(out),
            output_basename="tiny",
        )
        assert ok
        # Italic gets the -italic suffix on all three outputs.
        assert (out / "tiny-italic.ttf").exists()
        assert (out / "available-chars-italic.json").exists()
        assert (out / "available-features-italic.json").exists()
        # And the roman-named files should NOT appear.
        assert not (out / "tiny.ttf").exists()

    def test_missing_source_returns_false(self, tmp_path):
        ok = build_font(
            source_path=str(tmp_path / "no-such.glyphspackage"),
            output_dir=str(tmp_path / "out"),
            output_basename="x",
        )
        assert ok is False
