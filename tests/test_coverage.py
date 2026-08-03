"""Unit tests for GlyphAudit.coverage — reference→target gap analysis."""

from __future__ import annotations

import pytest

from GlyphAudit.coverage import (
    ABSENT,
    PRESENT_UNLINKED,
    UNENCODED_IN_TARGET,
    build_feature_file,
    coverage_gaps,
    feature_table,
    glyph_matrix,
    gsub_features_for_view,
    render_markdown,
)
from GlyphAudit.model import FontView


def _view(label, cmap, advances=None, variants=None, all_names=None):
    return FontView(
        label=label,
        source="/tmp/x",
        source_kind="ttf",
        upm=1000,
        cmap=cmap,
        advances=advances or {n: 500 for n in cmap.values()},
        gsub_variants=variants or {},
        all_glyph_names=all_names if all_names is not None else set(cmap.values()),
    )


REF = _view(
    "Ref",
    {0x61: "a", 0x62: "b", 0xA0: "uni00A0", 0xAD: "uni00AD"},
    variants={(0x61, "smcp"): "a.sc", (0x30, "onum"): "zero.osf"},
)


class TestCodepointGaps:
    def test_full_coverage_has_no_gaps(self):
        tgt = _view("T", dict(REF.cmap), variants=dict(REF.gsub_variants))
        r = coverage_gaps(tgt, REF, "Regular")
        assert r.codepoint_gaps == []
        assert r.variant_gaps == []
        assert r.absent_count() == 0

    def test_absent_codepoint(self):
        cmap = {k: v for k, v in REF.cmap.items() if k != 0x62}
        tgt = _view("T", cmap)
        r = coverage_gaps(tgt, REF)
        assert len(r.codepoint_gaps) == 1
        gap = r.codepoint_gaps[0]
        assert gap.codepoint == 0x62
        assert gap.kind == ABSENT
        assert gap.unicode_name == "LATIN SMALL LETTER B"
        assert gap.block == "Basic Latin"

    def test_unencoded_but_present_by_name(self):
        # Target lacks U+00A0 in its cmap but has the glyph by name.
        cmap = {k: v for k, v in REF.cmap.items() if k != 0xA0}
        tgt = _view(
            "T", cmap,
            variants=dict(REF.gsub_variants),
            all_names=set(cmap.values()) | {"uni00A0"},
        )
        r = coverage_gaps(tgt, REF)
        kinds = {g.codepoint: g.kind for g in r.codepoint_gaps}
        assert kinds[0xA0] == UNENCODED_IN_TARGET
        # Warnings don't fail the run.
        assert r.absent_count() == 0

    def test_extra_target_codepoints_are_ignored(self):
        cmap = dict(REF.cmap)
        cmap[0x1F600] = "emoji"
        tgt = _view("T", cmap)
        r = coverage_gaps(tgt, REF)
        assert r.codepoint_gaps == []


class TestVariantGaps:
    def test_absent_variant(self):
        tgt = _view("T", dict(REF.cmap))
        r = coverage_gaps(tgt, REF)
        assert {(g.base_codepoint, g.feature) for g in r.variant_gaps} == {
            (0x61, "smcp"), (0x30, "onum"),
        }
        assert all(g.kind == ABSENT for g in r.variant_gaps)

    def test_present_unlinked_variant(self):
        # Target has the variant glyph by name but no feature mapping.
        tgt = _view(
            "T", dict(REF.cmap),
            all_names=set(REF.cmap.values()) | {"a.sc"},
        )
        r = coverage_gaps(tgt, REF)
        kinds = {(g.base_codepoint, g.feature): g.kind for g in r.variant_gaps}
        assert kinds[(0x61, "smcp")] == PRESENT_UNLINKED
        assert kinds[(0x30, "onum")] == ABSENT
        # One true absence remains → fails.
        assert r.absent_count() == 1


class TestReverseGaps:
    def test_reverse_direction(self):
        from GlyphAudit.coverage import reverse_gaps
        # Target has c/0x63 and an ss01 variant the reference lacks.
        cmap = dict(REF.cmap)
        cmap[0x63] = "c"
        tgt = _view("T", cmap, variants={(0x61, "ss01"): "a.alt"})
        rev = reverse_gaps(tgt, REF, "Regular")
        cps = {g.codepoint: g.kind for g in rev.codepoint_gaps}
        assert cps == {0x63: ABSENT}
        vars_ = {(g.base_codepoint, g.feature) for g in rev.variant_gaps}
        assert vars_ == {(0x61, "ss01")}
        # Labels follow the forward framing.
        assert rev.target_label == "T"
        assert rev.reference_label == "Ref"

    def test_reverse_section_in_report(self):
        from GlyphAudit.coverage import reverse_gaps
        cmap = dict(REF.cmap)
        cmap[0x63] = "c"
        tgt = _view("Target Regular", cmap, variants=dict(REF.gsub_variants))
        r = coverage_gaps(tgt, REF, "Regular")
        r.reverse = reverse_gaps(tgt, REF, "Regular")
        md = render_markdown([r])
        assert "### Reverse — in Target Regular but not in Ref" in md
        assert "U+0063" in md
        assert "Informational" in md


class TestReport:
    def test_markdown_structure(self):
        cmap = {k: v for k, v in REF.cmap.items() if k != 0x62}
        tgt = _view("Target Regular", cmap)
        md = render_markdown([coverage_gaps(tgt, REF, "Regular")])
        assert "## Regular — Target Regular vs Ref" in md
        assert "**FAIL**" in md
        assert "U+0062" in md
        assert "c2sc" not in md  # no such gaps in this fixture
        assert "#### smcp" in md
        assert "#### onum" in md

    def test_pass_report(self):
        tgt = _view("T", dict(REF.cmap), variants=dict(REF.gsub_variants))
        md = render_markdown([coverage_gaps(tgt, REF, "Regular")])
        assert "**PASS**" in md
        assert "No gaps" in md


class TestFeatureTable:
    def test_statuses(self):
        # Target serves smcp fully, onum partially, nothing else.
        tgt = _view(
            "T", dict(REF.cmap),
            variants={(0x61, "smcp"): "a.sc"},
        )
        rows = {r.feature: r for r in feature_table(tgt, REF)}
        assert rows["smcp"].status == "full"
        assert rows["smcp"].ref_rules == 1
        assert rows["onum"].status == "missing"
        # A feature the target has but the reference lacks is reported.
        tgt2 = _view(
            "T", dict(REF.cmap),
            variants={(0x61, "smcp"): "a.sc", (0x61, "ss01"): "a.alt"},
        )
        rows2 = {r.feature: r for r in feature_table(tgt2, REF)}
        assert rows2["ss01"].ref_rules == 0
        assert rows2["ss01"].status == "missing"


class TestGlyphMatrix:
    def test_union_with_yes_no(self):
        cmap_t = dict(REF.cmap)
        del cmap_t[0x62]
        cmap_t[0x63] = "c"
        tgt = _view("T", cmap_t, variants={(0x61, "smcp"): "a.sc"})
        cp_rows, var_rows = glyph_matrix(tgt, REF)
        by_label = {m.label: m for m in cp_rows}
        assert by_label["b (U+0062)"].target_name is None
        assert by_label["b (U+0062)"].ref_name == "b"
        assert by_label["c (U+0063)"].target_name == "c"
        assert by_label["c (U+0063)"].ref_name is None
        var_by_label = {m.label: m for m in var_rows}
        assert var_by_label["a · smcp"].target_name == "a.sc"
        assert var_by_label["a · smcp"].ref_name == "a.sc"
        assert var_by_label["0 · onum"].target_name is None


class TestBuildFeatureFile:
    def test_rules_rewritten_to_target_names(self):
        # Reference names differ from target names; rewriting must go
        # through the codepoint, not the name.
        ref = _view("Ref", {0x61: "uni0061"})
        tgt = _view("T", {0x61: "a"}, variants={(0x61, "smcp"): "a.smcp"})
        gsub_map = {"smcp": {"uni0061": "uni0061.sc"}}
        fea, stats = build_feature_file(tgt, ref, gsub_map=gsub_map, lig_map={})
        assert "sub a by a.smcp;" in fea
        assert stats["rules"] == 1
        assert stats["skipped"] == 0

    def test_missing_glyphs_are_skipped(self):
        ref = _view("Ref", {0x61: "a", 0x62: "b"})
        tgt = _view("T", {0x61: "a"})
        gsub_map = {"smcp": {"a": "a.sc", "b": "b.sc"}}
        fea, stats = build_feature_file(tgt, ref, gsub_map=gsub_map, lig_map={})
        assert "sub a by" not in fea  # target has no a.sc variant
        assert stats["skipped"] == 2
        assert stats["skipped_features"] == ["smcp"]

    def test_ligature_rule(self):
        ref = _view("Ref", {0x66: "f", 0x69: "i"})
        tgt = _view(
            "T", {0x66: "f", 0x69: "i"},
            all_names={"f", "i", "f_i"},
        )
        lig_map = {"dlig": [(("f", "i"), "f_i")]}
        fea, stats = build_feature_file(tgt, ref, gsub_map={}, lig_map=lig_map)
        assert "sub f i by f_i;" in fea
        assert stats["rules"] == 1


class TestCompiledGsubRead:
    def test_reads_single_substs(self, tmp_path):
        """Build a minimal TTF with feaLib and read the GSUB back."""
        fontTools = pytest.importorskip("fontTools")
        from fontTools.fontBuilder import FontBuilder
        from fontTools.feaLib.builder import addOpenTypeFeaturesFromString
        from fontTools.ttLib.tables._g_l_y_f import Glyph as TTGlyph

        fb = FontBuilder(1000, isTTF=True)
        order = [".notdef", "a", "a.sc"]
        fb.setupGlyphOrder(order)
        fb.setupCharacterMap({0x61: "a"})
        fb.setupGlyf({g: TTGlyph() for g in order})
        fb.setupHorizontalMetrics({g: (500, 0) for g in order})
        fb.setupHorizontalHeader()
        fb.setupNameTable({"familyName": "T", "styleName": "R"})
        fb.setupOS2()
        fb.setupPost()
        fb.save(tmp_path / "base.ttf")
        font = fontTools.ttLib.TTFont(tmp_path / "base.ttf")
        addOpenTypeFeaturesFromString(
            font,
            "feature smcp { sub a by a.sc; } smcp;",
        )
        font.save(tmp_path / "feat.ttf")

        view = _view("Ref", {0x61: "a"})
        view.source = str(tmp_path / "feat.ttf")
        view.source_kind = "ttf"
        assert gsub_features_for_view(view) == {"smcp": {"a": "a.sc"}}
