"""Unit tests for GlyphAudit.proof.build helpers.

Fast tests that don't require fontc — they exercise the pieces of the
build pipeline that stand on their own: filename derivation and
`write_proof_config` (schema + reference-copying).

`build_font` itself is exercised in `test_build_integration.py`, which
does require fontc and is skipped when it's missing.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from GlyphAudit.proof.build import output_paths_for, write_proof_config
from GlyphAudit.proof.config import Reference, ReferenceSlot


# ---------------------------------------------------------------------------
# output_paths_for
# ---------------------------------------------------------------------------

class TestOutputPathsFor:
    def test_roman_no_suffix(self):
        assert output_paths_for("Foo.glyphspackage", "foo-proof") == (
            "foo-proof.ttf",
            "available-chars.json",
            "available-features.json",
        )

    def test_italic_gets_suffix(self):
        assert output_paths_for("Foo Italic.glyphspackage", "foo-proof") == (
            "foo-proof-italic.ttf",
            "available-chars-italic.json",
            "available-features-italic.json",
        )

    def test_case_insensitive(self):
        assert output_paths_for("Foo ITALIC.glyphspackage", "x")[0] == "x-italic.ttf"
        assert output_paths_for("Foo italic.glyphspackage", "x")[0] == "x-italic.ttf"

    def test_italic_substring_anywhere(self):
        # Real projects sometimes use "-Italic-Working" or "MyItalicPkg"
        # naming; the detection is intentionally lenient.
        assert output_paths_for("MyItalicPkg.glyphspackage", "x")[0] == "x-italic.ttf"

    def test_roman_family_named_italic_free_of_marker(self):
        # Sanity: a purely roman family with no italic in the name gets
        # the unsuffixed paths.
        assert output_paths_for("Roman.glyphspackage", "r")[0] == "r.ttf"


# ---------------------------------------------------------------------------
# write_proof_config
# ---------------------------------------------------------------------------

class TestWriteProofConfig:
    def test_basic_manifest_shape(self, tmp_path):
        cfg = write_proof_config(
            output_dir=str(tmp_path),
            family_name="TinyProof",
            output_basename="tiny",
            sources=["Tiny.glyphspackage"],
            references=(),
        )
        assert cfg["familyName"] == "TinyProof"
        assert cfg["faces"] == {
            "roman": {
                "ttf": "/tiny.ttf",
                "chars": "/available-chars.json",
                "features": "/available-features.json",
            }
        }
        assert cfg["references"] == []
        # File landed on disk with matching content.
        on_disk = json.loads((tmp_path / "proof-config.json").read_text())
        assert on_disk == cfg

    def test_italic_and_roman_both_populated(self, tmp_path):
        cfg = write_proof_config(
            output_dir=str(tmp_path),
            family_name="X",
            output_basename="x",
            sources=["X.glyphspackage", "X Italic.glyphspackage"],
        )
        assert "roman" in cfg["faces"]
        assert "italic" in cfg["faces"]
        assert cfg["faces"]["italic"]["ttf"] == "/x-italic.ttf"

    def test_reference_slots_serialised(self, tmp_path):
        ref_dir = tmp_path / "refs"
        ref_dir.mkdir()
        (ref_dir / "Roman.ttf").write_bytes(b"stub-ttf")
        (ref_dir / "Italic.ttf").write_bytes(b"stub-ttf-italic")
        ref = Reference(
            name="Sample",
            slots=(
                ReferenceSlot("regular", str(ref_dir / "Roman.ttf"),  400, "normal"),
                ReferenceSlot("italic",  str(ref_dir / "Italic.ttf"), 400, "italic"),
            ),
        )
        cfg = write_proof_config(
            output_dir=str(tmp_path),
            family_name="X",
            output_basename="x",
            sources=["X.glyphspackage"],
            references=(ref,),
        )
        assert cfg["references"] == [{
            "name": "Sample",
            "slots": [
                {"file": "/Roman.ttf",  "weight": 400, "style": "normal"},
                {"file": "/Italic.ttf", "weight": 400, "style": "italic"},
            ],
        }]

    def test_reference_ttfs_copied(self, tmp_path):
        ref_dir = tmp_path / "refs"; ref_dir.mkdir()
        src = ref_dir / "Ref.ttf"; src.write_bytes(b"payload")
        out_dir = tmp_path / "out"; out_dir.mkdir()

        ref = Reference(name="R", slots=(ReferenceSlot("regular", str(src), 400, "normal"),))
        write_proof_config(
            output_dir=str(out_dir),
            family_name="X",
            output_basename="x",
            sources=["X.glyphspackage"],
            references=(ref,),
        )
        dest = out_dir / "Ref.ttf"
        assert dest.exists()
        assert dest.read_bytes() == b"payload"

    def test_reference_copy_is_idempotent(self, tmp_path):
        # Rebuilding shouldn't rewrite reference TTFs when they haven't
        # changed — that would bump mtime on every build and disrupt any
        # dev tools watching the output directory.
        ref_dir = tmp_path / "refs"; ref_dir.mkdir()
        src = ref_dir / "Ref.ttf"; src.write_bytes(b"payload")
        out_dir = tmp_path / "out"; out_dir.mkdir()
        ref = Reference(name="R", slots=(ReferenceSlot("regular", str(src), 400, "normal"),))

        write_proof_config(
            output_dir=str(out_dir), family_name="X", output_basename="x",
            sources=["X.glyphspackage"], references=(ref,),
        )
        dest = out_dir / "Ref.ttf"
        first_mtime = dest.stat().st_mtime

        # Sleep just past filesystem mtime resolution before the second call —
        # HFS+/APFS both use 1 s resolution on macOS for some ops.
        time.sleep(1.1)
        write_proof_config(
            output_dir=str(out_dir), family_name="X", output_basename="x",
            sources=["X.glyphspackage"], references=(ref,),
        )
        assert dest.stat().st_mtime == first_mtime

    def test_reference_recopy_when_source_newer(self, tmp_path):
        ref_dir = tmp_path / "refs"; ref_dir.mkdir()
        src = ref_dir / "Ref.ttf"; src.write_bytes(b"v1")
        out_dir = tmp_path / "out"; out_dir.mkdir()
        ref = Reference(name="R", slots=(ReferenceSlot("regular", str(src), 400, "normal"),))
        write_proof_config(
            output_dir=str(out_dir), family_name="X", output_basename="x",
            sources=["X.glyphspackage"], references=(ref,),
        )
        dest = out_dir / "Ref.ttf"
        assert dest.read_bytes() == b"v1"

        # Modify the source (and push its mtime forward).
        time.sleep(1.1)
        src.write_bytes(b"v2")

        write_proof_config(
            output_dir=str(out_dir), family_name="X", output_basename="x",
            sources=["X.glyphspackage"], references=(ref,),
        )
        assert dest.read_bytes() == b"v2"

    def test_missing_reference_logged_not_fatal(self, tmp_path, capsys):
        # A stale config referencing a deleted TTF shouldn't crash the build —
        # the manifest still gets written; the missing slot is noted.
        ref = Reference(
            name="Gone",
            slots=(ReferenceSlot("regular", "/nowhere/nothing.ttf", 400, "normal"),),
        )
        cfg = write_proof_config(
            output_dir=str(tmp_path), family_name="X", output_basename="x",
            sources=["X.glyphspackage"], references=(ref,),
        )
        # Reference entry survives — the web app displays "(missing)" for the
        # slot rather than dropping the whole reference.
        assert cfg["references"][0]["name"] == "Gone"

    def test_variable_slot_style_preserved(self, tmp_path):
        ref_dir = tmp_path / "refs"; ref_dir.mkdir()
        var = ref_dir / "Var.ttf"; var.write_bytes(b"stub")
        ref = Reference(
            name="Var",
            slots=(ReferenceSlot("variable", str(var), 0, "variable"),),
        )
        cfg = write_proof_config(
            output_dir=str(tmp_path), family_name="X", output_basename="x",
            sources=["X.glyphspackage"], references=(ref,),
        )
        entry = cfg["references"][0]["slots"][0]
        assert entry["style"] == "variable"

    def test_creates_output_dir(self, tmp_path):
        # A fresh checkout might not have `proof-app/public/` yet — the
        # helper should create it, not error.
        nested = tmp_path / "does" / "not" / "exist"
        write_proof_config(
            output_dir=str(nested), family_name="X", output_basename="x",
            sources=["X.glyphspackage"],
        )
        assert (nested / "proof-config.json").is_file()
