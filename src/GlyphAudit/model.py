"""Data model for font coverage comparison."""

from dataclasses import dataclass, field
from typing import Optional


# Glyphs colour-index → friendly name. Used for --filter.
GLYPHS_COLOR_NAMES: dict[int, str] = {
    0: "red",
    1: "orange",
    2: "brown",
    3: "yellow",
    4: "light-green",
    5: "green",
    6: "light-blue",
    7: "blue",
    8: "purple",
    9: "pink",
    10: "light-gray",
    11: "gray",
}

# Filter spec name → set of colour indices that pass. One entry per palette
# colour (derived from GLYPHS_COLOR_NAMES so the two can't drift), plus:
#   ready     — yellow OR light-green, the project convention for
#               "flagged for proofing"
#   no-colour — glyphs with no colour label at all (colors dict has no
#               entry, so the lookup yields None)
COLOR_FILTERS: dict[str, set] = {
    name: {idx} for idx, name in GLYPHS_COLOR_NAMES.items()
}
COLOR_FILTERS["ready"] = {3, 4}       # yellow OR light-green
COLOR_FILTERS["no-colour"] = {None}


# Suffixes used in target glyph names that map to OpenType feature tags.
# These are the variants we can match across fonts even when the variant
# itself isn't encoded.
FEATURE_SUFFIXES: dict[str, str] = {
    "smcp":   "smcp",
    "c2sc":   "c2sc",
    "ss01":   "ss01",
    "ss02":   "ss02",
    "ss03":   "ss03",
    "ss04":   "ss04",
    "ss05":   "ss05",
    "ss06":   "ss06",
    "ss07":   "ss07",
    "ss08":   "ss08",
    "ss09":   "ss09",
    "ss10":   "ss10",
    "onum":   "onum",
    "pnum":   "pnum",
    "lnum":   "lnum",
    "tnum":   "tnum",
    "numr":   "numr",
    "dnom":   "dnom",
    "sinf":   "sinf",
    "sups":   "sups",
    "ordn":   "ordn",
    "salt":   "salt",
    "frac":   "frac",
    "frac0":  "frac",  # Glyphs convention for the bottom-pos numerator
    "osf":    "onum",  # oldstyle figures (alternate spelling)
    "tosf":   "onum",  # tabular oldstyle figures (alternate spelling)
}


@dataclass
class FontView:
    """Normalized view over one font instance (one weight).

    Loaders produce these; the comparator and reporter consume them.
    All advance values are in font units (UPM-scaled by the loader if
    cross-UPM normalization is needed by the caller).
    """

    label: str
    """Human-readable label, e.g. 'Helvetica Regular'."""

    source: str
    """Path or system marker — for the report's provenance line."""

    source_kind: str
    """One of 'ttf', 'glyphs', 'system'."""

    upm: int
    """Units per em."""

    cmap: dict[int, str] = field(default_factory=dict)
    """codepoint -> glyph name."""

    advances: dict[str, int] = field(default_factory=dict)
    """glyph name -> advance width in font units. May be missing for
    unencoded glyphs that the loader couldn't measure."""

    left_sidebearings: dict[str, int] = field(default_factory=dict)
    """glyph name -> LSB in font units. Empty for loaders that don't
    supply bearings (older Glyphs-source loaders, some system-font paths).
    The width-audit UI hides the LSB/RSB columns when this is empty on
    either side."""

    right_sidebearings: dict[str, int] = field(default_factory=dict)
    """glyph name -> RSB in font units. Same caveats as `left_sidebearings`."""

    gsub_variants: dict[tuple[int, str], str] = field(default_factory=dict)
    """(source_codepoint, feature_tag) -> variant glyph name. Built either
    from compiled GSUB (TTF/system) or from name-suffix conventions
    (Glyphs sources)."""

    all_glyph_names: set[str] = field(default_factory=set)
    """Every glyph name present in the font, encoded or not."""

    colors: dict[str, int] = field(default_factory=dict)
    """glyph name -> Glyphs colour index (0–11). Empty for non-Glyphs
    sources. Conventional usage: 3 = yellow / ready, 4 = light green /
    passed inspection."""

    def advance_for_codepoint(self, cp: int) -> Optional[int]:
        name = self.cmap.get(cp)
        if name is None:
            return None
        return self.advances.get(name)

    def advance_for_variant(self, cp: int, feature: str) -> Optional[int]:
        name = self.gsub_variants.get((cp, feature))
        if name is None:
            return None
        return self.advances.get(name)

    def variant_name(self, cp: int, feature: str) -> Optional[str]:
        return self.gsub_variants.get((cp, feature))


def parse_variant_suffix(glyph_name: str) -> Optional[tuple[str, str]]:
    """Split 'a.smcp' or 'I-cy.ss01' into (base_name, feature_tag).

    Returns None if the trailing suffix is not a recognised feature.
    Multi-suffix forms ('zero.osf.slash') walk inwards: returns the
    deepest known feature.
    """
    parts = glyph_name.split(".")
    if len(parts) < 2:
        return None
    # Walk from the end inward, stripping unknown suffixes (e.g. .case, .alt)
    # until we find a known feature.
    for i in range(len(parts) - 1, 0, -1):
        suffix = parts[i]
        if suffix in FEATURE_SUFFIXES:
            base = ".".join(parts[:i])
            return (base, FEATURE_SUFFIXES[suffix])
    return None
