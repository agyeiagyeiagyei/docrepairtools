"""Shared test fixtures for the proof subpackage.

`tiny_source_factory` writes a minimal `.glyphspackage` into a
`tmp_path` — enough glyphs + features + colours to exercise the
build library's key behaviours without shipping a large blob in-tree.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

import pytest

# Make the src/ layout importable without pip-installing during tests.
_HERE = Path(__file__).resolve()
_SRC = _HERE.parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ---------------------------------------------------------------------------
# .glyphspackage writer
# ---------------------------------------------------------------------------

# Minimal .glyph body. Colour is optional; 400-unit advance keeps math simple.
# fontc rejects a layer that has no `shapes` (treats it as "no instances");
# a single degenerate closed-square shape satisfies that check for glyphs
# that don't declare their own contours. Component-only glyphs (composites)
# get `shapes = ({ref = X;},…)` inline instead — see `_glyph_body`.
_STUB_SQUARE = """shapes = (
{
closed = 1;
nodes = (
(0,0,l),
(100,0,l),
(100,100,l),
(0,100,l)
);
}
);
"""

_GLYPH_TEMPLATE = """{{
glyphname = {name};
{extras}layers = (
{{
layerId = m01;
{shapes}width = 400;
}}
);
unicode = {unicode};
}}
"""

_GLYPH_TEMPLATE_NO_UNICODE = """{{
glyphname = {name};
{extras}layers = (
{{
layerId = m01;
{shapes}width = 400;
}}
);
}}
"""

# fontinfo.plist — one master, one axis, one instance. Optional `code`
# gets spliced into the features array so tests can inject a broken
# feature and assert it gets filtered.
_FONTINFO_TEMPLATE = """{{
.formatVersion = 3;
axes = (
{{
name = Weight;
tag = wght;
}}
);
familyName = "{family}";
fontMaster = (
{{
axesValues = (
400
);
id = m01;
metricValues = (
{{
pos = 800;
}},
{{
pos = -200;
}}
);
name = Regular;
}}
);
instances = (
{{
axesValues = (
400
);
name = Regular;
weightClass = 400;
}}
);
metrics = (
{{
type = ascender;
}},
{{
type = descender;
}}
);
unitsPerEm = 1000;
versionMajor = 1;
versionMinor = 0;
{features}}}
"""


def _glyph_body(
    name: str,
    unicode_hex: Optional[str],
    color: Optional[int] = None,
    component_refs: tuple[str, ...] = (),
) -> str:
    """Return a valid .glyph plist body."""
    extras = ""
    if color is not None:
        extras = f"color = {color};\n"
    if component_refs:
        shape_entries = "".join(
            f"""{{
ref = {ref};
}},
"""
            for ref in component_refs
        )
        shapes = f"""shapes = (
{shape_entries});
"""
    else:
        # Every layer needs at least one shape or fontc rejects it as
        # "no instances". A tiny throwaway square satisfies that without
        # affecting anything the tests care about.
        shapes = _STUB_SQUARE
    if unicode_hex is None:
        return _GLYPH_TEMPLATE_NO_UNICODE.format(
            name=name, extras=extras, shapes=shapes,
        )
    return _GLYPH_TEMPLATE.format(
        name=name, extras=extras, shapes=shapes, unicode=unicode_hex,
    )


# Default per-glyph specification. Tests can override.
# (name, unicode_decimal, color_index, component_refs)
# NOTE: Glyphs.app writes unicode as *decimal* in its .glyph plists (not hex,
# despite looking hex-y in glyph documentation). `_parse_glyph` reads via
# `\d+` so the fixture must supply decimal codepoints or every glyph ends up
# at the wrong slot in the cmap.
_DEFAULT_GLYPHS: tuple[tuple[str, Optional[str], Optional[int], tuple[str, ...]], ...] = (
    ("_notdef", None,     None, ()),          # essential, always kept
    ("space",   "32",     None, ()),          # U+0020 — essential, always kept
    ("a",       "97",     3,    ()),          # U+0061 — yellow → kept
    ("b",       "98",     4,    ()),          # U+0062 — light green → kept
    ("c",       "99",     0,    ()),          # U+0063 — red → filtered out
    ("d",       "100",    None, ()),          # U+0064 — no colour → filtered out
    ("A",       "65",     3,    ("a", "acomb")),  # U+0041 — composite of /a + /acomb
    ("acomb",   None,     None, ()),          # uncoloured mark — pulled in
                                              # transitively when /A is kept
)


def write_tiny_glyphspackage(
    pkg_path: Path,
    *,
    family_name: str = "TestFamily",
    glyphs=None,
    features_block: str = "",
) -> Path:
    """Materialise a minimal .glyphspackage on disk under `pkg_path`.

    Args:
        pkg_path:      directory to create (parent must exist).
        family_name:   embedded in fontinfo.plist; italic behaviour keys off
                       the *filename*, so callers testing italic-detection
                       should name the directory `<X> Italic.glyphspackage`.
        glyphs:        override sequence of (name, unicode, colour, refs).
                       Falls back to `_DEFAULT_GLYPHS`.
        features_block: raw plist text spliced into fontinfo.plist's top-
                       level. Tests use this to add a `features = (…);`
                       array with intentionally broken rules.

    Returns `pkg_path`.
    """
    pkg_path = Path(pkg_path)
    glyphs_dir = pkg_path / "glyphs"
    glyphs_dir.mkdir(parents=True, exist_ok=True)

    spec = glyphs if glyphs is not None else _DEFAULT_GLYPHS
    order_names = []
    for name, unicode_hex, color, refs in spec:
        body = _glyph_body(name, unicode_hex, color, refs)
        # Glyphs's on-disk convention: each uppercase letter gets an
        # underscore suffix so case-insensitive filesystems (macOS default)
        # can carry `/a.glyph` and `/A_.glyph` without collision. Applied
        # per character so `AE` becomes `A_E_.glyph`.
        fname = "".join(c + "_" if c.isupper() else c for c in name) + ".glyph"
        (glyphs_dir / fname).write_text(body, encoding="utf-8")
        order_names.append(name)

    (pkg_path / "fontinfo.plist").write_text(
        _FONTINFO_TEMPLATE.format(family=family_name, features=features_block),
        encoding="utf-8",
    )
    order_body = "(\n" + ",\n".join(order_names) + "\n)\n"
    (pkg_path / "order.plist").write_text(order_body, encoding="utf-8")
    return pkg_path


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tiny_source_factory(tmp_path):
    """Factory: `tiny_source_factory(name="Tiny.glyphspackage", **kw)` returns
    a fresh .glyphspackage under `tmp_path`. Each call creates a distinct
    package so a single test can build both roman and italic sources.
    """
    def make(name="Tiny.glyphspackage", **kwargs):
        return write_tiny_glyphspackage(tmp_path / name, **kwargs)
    return make


@pytest.fixture
def tiny_source(tiny_source_factory):
    """Convenience: a single default source. Most build tests want this."""
    return tiny_source_factory()
