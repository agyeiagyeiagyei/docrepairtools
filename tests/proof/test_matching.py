"""Unit tests for the panel's system-font pairing helpers.

These stay AppKit-free — pure input/output. The NSFontManager side of the
picker (which needs macOS + a live font system) is exercised by the L5
manual smoke checklist.
"""

from __future__ import annotations

import pytest

from GlyphAudit.proof.panel.matching import (
    infer_italic,
    infer_weight,
    master_to_target,
    pick_best_member,
    score_member,
)


class TestInferWeight:
    @pytest.mark.parametrize("name,expected", [
        ("Regular", 400),
        ("regular", 400),
        ("Bold", 700),
        ("Italic", 400),
        ("Bold Italic", 700),
        ("Thin", 100),
        ("Ultralight", 100),
        ("Extralight", 200),
        ("Light", 300),
        ("Book", 400),
        ("Medium", 500),
        ("Semibold", 600),
        ("Demibold", 600),
        ("Bold Italic", 700),
        ("Extrabold", 800),
        ("Black", 900),
        ("Heavy", 900),
        # No weight token → default 400
        ("Text", 400),
        ("Display", 400),
        ("", 400),
    ])
    def test_maps_correctly(self, name, expected):
        assert infer_weight(name) == expected

    def test_extralight_beats_light(self):
        # Longest-first ordering means `extralight` matches before `light`
        # even though `light` is a substring — otherwise Extralight gets
        # misread as weight 300.
        assert infer_weight("Extralight Italic") == 200
        assert infer_weight("Ultralight") == 100


class TestInferItalic:
    @pytest.mark.parametrize("name,expected", [
        ("Regular", False),
        ("Italic", True),
        ("Bold Italic", True),
        ("Bold", False),
        ("Oblique", True),        # oblique treated as italic-equivalent
        ("Bold Oblique", True),
        ("", False),
    ])
    def test_maps_correctly(self, name, expected):
        assert infer_italic(name) == expected


class TestScoreMember:
    def test_exact_match_scores_highest(self):
        # Bold master vs Bold face — best possible pairing.
        s_exact = score_member("Bold", "Bold")
        s_close = score_member("Bold", "Semibold")
        s_wrong = score_member("Bold", "Regular")
        assert s_exact > s_close > s_wrong

    def test_italic_mismatch_hurts_more_than_weight(self):
        # Given a Bold Italic master, an italic-mismatched face at the
        # right weight should score LOWER than an italic-matched face at
        # a slightly-off weight. Italic parity is load-bearing.
        s_right_weight_wrong_ital = score_member("Bold Italic", "Bold")
        s_right_ital_close_weight = score_member("Bold Italic", "Semibold Italic")
        assert s_right_ital_close_weight > s_right_weight_wrong_ital

    def test_regular_defaults_lose_to_explicit_weight(self):
        # When the master name is Bold, "Bold" beats "Regular" — of course.
        # This anchors the tie-break: if two faces both score, the one
        # with a non-default weight token wins.
        assert score_member("Bold", "Bold") > score_member("Bold", "Regular")

    def test_regular_master_prefers_regular_face(self):
        # Sanity: default-weight master pairs to Regular over Bold.
        assert score_member("Regular", "Regular") > score_member("Regular", "Bold")

    def test_oblique_matches_italic_master(self):
        # Some system fonts (Helvetica) still use "Oblique". A Bold Italic
        # master should still pair with Bold Oblique.
        s_ital = score_member("Bold Italic", "Bold Oblique")
        s_upright = score_member("Bold Italic", "Bold")
        assert s_ital > s_upright


class TestPickBestMember:
    def test_verdana_bold_italic(self):
        # A realistic Verdana-family enumeration. Master = Bold Italic
        # should pick Bold Italic, not Bold or Regular.
        members = [
            ("Verdana",             "Regular"),
            ("Verdana-Bold",        "Bold"),
            ("Verdana-Italic",      "Italic"),
            ("Verdana-BoldItalic",  "Bold Italic"),
        ]
        ps, face = pick_best_member("Bold Italic", members)
        assert ps == "Verdana-BoldItalic"
        assert face == "Bold Italic"

    def test_verdana_regular_master(self):
        members = [
            ("Verdana",             "Regular"),
            ("Verdana-Bold",        "Bold"),
            ("Verdana-Italic",      "Italic"),
            ("Verdana-BoldItalic",  "Bold Italic"),
        ]
        ps, _ = pick_best_member("Regular", members)
        assert ps == "Verdana"

    def test_single_member_family_returns_it(self):
        # Family with just one face — no choice, take what's there.
        members = [("Zapfino", "Regular")]
        assert pick_best_member("Bold Italic", members)[0] == "Zapfino"

    def test_empty_members_returns_none(self):
        assert pick_best_member("Regular", []) is None

    def test_quirky_master_name_falls_back_to_regular(self):
        # Master called "Display" (a common design axis) has no weight or
        # italic tokens; picker should land on Regular rather than the
        # heavier options.
        members = [
            ("SomeFont-Bold",       "Bold"),
            ("SomeFont",            "Regular"),
            ("SomeFont-BoldItalic", "Bold Italic"),
        ]
        ps, _ = pick_best_member("Display", members)
        assert ps == "SomeFont"

    def test_master_with_light_matches_light(self):
        members = [
            ("MyFont-Regular", "Regular"),
            ("MyFont-Light",   "Light"),
            ("MyFont-Bold",    "Bold"),
        ]
        assert pick_best_member("Light", members)[0] == "MyFont-Light"

    def test_helvetica_oblique_pairing(self):
        # Realistic Helvetica enumeration — Italic master should pair with
        # Oblique, not Italic (Helvetica ships Oblique, not Italic).
        members = [
            ("Helvetica",             "Regular"),
            ("Helvetica-Bold",        "Bold"),
            ("Helvetica-Oblique",     "Oblique"),
            ("Helvetica-BoldOblique", "Bold Oblique"),
        ]
        ps, _ = pick_best_member("Italic", members)
        assert ps == "Helvetica-Oblique"
        ps, _ = pick_best_member("Bold Italic", members)
        assert ps == "Helvetica-BoldOblique"


class TestMasterToTarget:
    @pytest.mark.parametrize("master,expected_weight,expected_italic", [
        ("Regular",       400, False),
        ("Bold",          700, False),
        ("Italic",        400, True),
        ("Bold Italic",   700, True),
        ("Extralight Italic", 200, True),
        ("Semibold",      600, False),
    ])
    def test_matches_expectations(self, master, expected_weight, expected_italic):
        assert master_to_target(master) == (expected_weight, expected_italic)
