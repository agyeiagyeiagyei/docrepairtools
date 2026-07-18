"""Configuration schema for the glyph-audit proof pipeline.

A font project ships a `glyph-audit.toml` (or `.glyph-audit.toml`) at its
repo root describing what to build and what to compare against. Example:

    [project]
    name = "Merriweather"

    [proof]
    family_name = "Merriweather Proof"
    output_dir  = "proof-app/public"
    output_basename = "Merriweather-proof"
    sources = [
      "Merriweather.glyphspackage",
      "Merriweather Italic.glyphspackage",
    ]
    colors  = [3, 4]
    essential_glyphs = ["_notdef", "space"]   # optional

    [references.Georgia]
    regular      = "sources/reference/Georgia.ttf"
    bold         = "sources/reference/Georgia-Bold.ttf"
    italic       = "sources/reference/Georgia-Italic.ttf"
    bold_italic  = "sources/reference/Georgia-BoldItalic.ttf"

    [references."Source Serif 4"]
    variable = "~/fonts/SourceSerif4-Variable.ttf"

`glyph-audit proof serve` discovers this file by walking up from the
current directory. `~/.glyph-audit/config.toml` still holds width-audit
`[instances]` — proof config lives in the project file so different
font repos can carry different settings without collision.

Discovery order:
    1. CWD or any ancestor: `glyph-audit.toml` → `.glyph-audit.toml`
    2. Fallback: no project config (callers must supply defaults or fail).

The functions here parse + validate; they do NOT execute or build. Keep
the parser dependency-free so it can be reused from the Glyphs.app panel
running under the app's embedded Python.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import tomllib  # 3.11+
except ImportError:  # 3.9 / 3.10
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        tomllib = None  # type: ignore


class ConfigError(ValueError):
    """Raised when a discovered config file is malformed."""


# Glyphs.app color palette. Index → human label. Kept as an ordered list so
# the CLI and panel can render it in a stable order. "none" is a sentinel for
# uncolored glyphs (i.e. `color =` field absent from the .glyph file).
GLYPHS_COLORS: list[tuple[str, str]] = [
    ("0",   "Red"),
    ("1",   "Orange"),
    ("2",   "Brown"),
    ("3",   "Yellow"),
    ("4",   "Lt green"),
    ("5",   "Dk green"),
    ("6",   "Lt blue"),
    ("7",   "Dk blue"),
    ("8",   "Purple"),
    ("9",   "Pink"),
    ("10",  "Lt gray"),
    ("11",  "Dk gray"),
    ("none", "No color"),
]
_VALID_COLOR_KEYS: frozenset[str] = frozenset(k for k, _ in GLYPHS_COLORS)

# Yellow + light green = "ready for proofing" / "passed inspection" — the two
# flags most projects have used historically. Kept as the CLI default so
# existing invocations don't need to spell out `--colors 3,4`.
DEFAULT_PROOF_COLORS: frozenset[str] = frozenset({"3", "4"})

# Glyph names that must survive color filtering regardless of what the user
# picked — fontc needs both to compile a valid TTF.
DEFAULT_ESSENTIAL_GLYPHS: frozenset[str] = frozenset({"_notdef", "space"})


def normalize_color(value) -> str:
    """Coerce a color from int/str into the canonical string form.

    TOML lets users write `colors = [3, 4]` (ints) or `colors = ["3", "4"]`
    or the sentinel `["none"]`. This flattens all three into `"3"`, `"4"`,
    `"none"` so the rest of the pipeline never has to branch on type.
    """
    if isinstance(value, bool):
        # bool is an int subclass in Python — reject explicitly so a stray
        # `true`/`false` in TOML doesn't get silently coerced to "1"/"0".
        raise ConfigError(f"color must be int or str, got bool: {value!r}")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value.strip().lower()
    raise ConfigError(f"color must be int or str, got {type(value).__name__}: {value!r}")


def validate_colors(colors) -> frozenset[str]:
    """Coerce + validate an iterable of color values. Raises ConfigError on
    unknown keys. Returns an empty frozenset only if the input is empty —
    callers should treat that as "user wants nothing", i.e. no glyphs kept.
    """
    normalized = [normalize_color(c) for c in colors]
    bad = [c for c in normalized if c not in _VALID_COLOR_KEYS]
    if bad:
        valid = ", ".join(k for k, _ in GLYPHS_COLORS)
        raise ConfigError(f"invalid color keys: {bad!r}. Valid: {valid}")
    return frozenset(normalized)


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

# Style slots keyed by name. Web-app reference dropdowns emit these as CSS
# `@font-face` entries with the matching (weight, style) combination. Values
# aren't restricted here — the dict keys match TOML field names.
_REFERENCE_SLOT_META: dict[str, tuple[int, str]] = {
    "regular":     (400, "normal"),
    "bold":        (700, "normal"),
    "italic":      (400, "italic"),
    "bold_italic": (700, "italic"),
    # `variable` covers all four with a single file — the web app registers
    # multiple @font-face entries pointing at the same URL when it sees this.
    "variable":    (0,   "variable"),
}


@dataclass(frozen=True)
class ReferenceSlot:
    """A single (name, weight, style) → file mapping inside a Reference."""
    slot: str          # "regular" | "bold" | "italic" | "bold_italic" | "variable"
    path: str          # absolute POSIX path (already resolved relative to project root)
    weight: int        # 400 / 700 / 0 for variable
    style: str         # "normal" | "italic" | "variable"


@dataclass(frozen=True)
class Reference:
    """A named reference family with one or more style-slot files.

    In TOML: `[references.<name>]` with keys `regular`, `bold`, `italic`,
    `bold_italic`, or `variable`. At least one slot must be provided.
    """
    name: str
    slots: tuple[ReferenceSlot, ...]

    @property
    def has_italic(self) -> bool:
        return any(s.style == "italic" for s in self.slots) or any(
            s.slot == "variable" for s in self.slots
        )


# ---------------------------------------------------------------------------
# Proof section
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProofConfig:
    """Everything the build library needs to compile the proof TTFs.

    Frozen so it's safe to hand to subprocesses / capture in closures without
    worrying about mid-flight mutation.
    """
    family_name: str
    output_dir: str        # relative to project root, or absolute
    output_basename: str
    sources: tuple[str, ...]
    colors: frozenset[str]
    essential_glyphs: frozenset[str]
    references: tuple[Reference, ...]

    def italic_sources(self) -> tuple[str, ...]:
        return tuple(s for s in self.sources if "italic" in os.path.basename(s).lower())

    def roman_sources(self) -> tuple[str, ...]:
        return tuple(s for s in self.sources if "italic" not in os.path.basename(s).lower())


@dataclass(frozen=True)
class ProjectConfig:
    """The whole parsed config file, with proof section + provenance."""
    project_root: Path     # directory the config file was found in
    config_path: Path      # exact path to the file that was read
    name: Optional[str]    # [project] name = "…", if given
    proof: ProofConfig


# ---------------------------------------------------------------------------
# Discovery + parsing
# ---------------------------------------------------------------------------

_CONFIG_NAMES = ("glyph-audit.toml", ".glyph-audit.toml")


def _find_project_config(start: Path) -> Optional[Path]:
    """Walk up from `start` looking for `glyph-audit.toml` or its dot variant.

    Returns the first hit or None. Ordering is deepest-first (child overrides
    parent) — pip-installed monorepos with a top-level config and per-package
    overrides work out of the box.
    """
    start = start.resolve()
    for parent in [start] + list(start.parents):
        for name in _CONFIG_NAMES:
            candidate = parent / name
            if candidate.is_file():
                return candidate
    return None


def _parse_references(project_root: Path, refs_table: dict) -> tuple[Reference, ...]:
    """Convert TOML `[references.*]` entries into typed Reference objects."""
    out: list[Reference] = []
    for name, entry in refs_table.items():
        if not isinstance(entry, dict):
            raise ConfigError(f"[references.{name}] must be a table")
        slots: list[ReferenceSlot] = []
        for slot_key, slot_value in entry.items():
            if slot_key not in _REFERENCE_SLOT_META:
                # Silently skip unknown keys so config files stay forward-
                # compatible — future slot names (e.g. `condensed_bold`)
                # won't blow up existing tool versions.
                continue
            if not isinstance(slot_value, str):
                raise ConfigError(
                    f"[references.{name}].{slot_key} must be a path string, "
                    f"got {type(slot_value).__name__}"
                )
            weight, style = _REFERENCE_SLOT_META[slot_key]
            expanded = os.path.expanduser(slot_value)
            if not os.path.isabs(expanded):
                expanded = str((project_root / expanded).resolve())
            slots.append(ReferenceSlot(slot=slot_key, path=expanded, weight=weight, style=style))
        if not slots:
            raise ConfigError(
                f"[references.{name}] has no known slot keys "
                f"(regular / bold / italic / bold_italic / variable)"
            )
        out.append(Reference(name=name, slots=tuple(slots)))
    return tuple(out)


def _parse_proof(project_root: Path, data: dict) -> ProofConfig:
    """Parse the `[proof]` table into a ProofConfig with defaults filled in."""
    proof_table = data.get("proof")
    if proof_table is None:
        raise ConfigError("config file is missing a [proof] section")
    if not isinstance(proof_table, dict):
        raise ConfigError("[proof] must be a table")

    family_name = proof_table.get("family_name")
    if not isinstance(family_name, str) or not family_name.strip():
        raise ConfigError("[proof].family_name must be a non-empty string")

    output_dir = proof_table.get("output_dir", "proof-out")
    output_basename = proof_table.get("output_basename")
    if not isinstance(output_basename, str) or not output_basename.strip():
        # Fall back to family_name lowercased-hyphenated. Not automatic
        # because the user often wants control over the filename shape.
        default = family_name.replace(" ", "-").lower()
        output_basename = default

    sources = proof_table.get("sources", [])
    if not isinstance(sources, list) or not all(isinstance(s, str) for s in sources):
        raise ConfigError("[proof].sources must be a list of strings")
    if not sources:
        raise ConfigError("[proof].sources must include at least one source")

    raw_colors = proof_table.get("colors", list(DEFAULT_PROOF_COLORS))
    if not isinstance(raw_colors, list):
        raise ConfigError("[proof].colors must be a list")
    colors = validate_colors(raw_colors)
    if not colors:
        raise ConfigError("[proof].colors must include at least one color")

    essential = proof_table.get("essential_glyphs")
    if essential is None:
        essential_set = DEFAULT_ESSENTIAL_GLYPHS
    else:
        if not isinstance(essential, list) or not all(isinstance(s, str) for s in essential):
            raise ConfigError("[proof].essential_glyphs must be a list of strings")
        essential_set = frozenset(essential)

    refs_table = data.get("references") or {}
    if not isinstance(refs_table, dict):
        raise ConfigError("[references] must be a table")
    references = _parse_references(project_root, refs_table)

    return ProofConfig(
        family_name=family_name.strip(),
        output_dir=str(output_dir),
        output_basename=output_basename.strip(),
        sources=tuple(sources),
        colors=colors,
        essential_glyphs=essential_set,
        references=references,
    )


def load_project_config(start: Optional[Path] = None) -> Optional[ProjectConfig]:
    """Locate + parse the nearest `glyph-audit.toml`. Returns None if no
    config file is found. Raises `ConfigError` on a malformed file — we
    prefer loud failures over silent fallbacks when the user *did* write
    a config and got a key wrong.
    """
    if tomllib is None:
        raise ConfigError(
            "TOML parser unavailable. Install `tomli` (Python 3.9/3.10) or "
            "upgrade to Python 3.11+."
        )
    start = Path(start) if start is not None else Path.cwd()
    config_path = _find_project_config(start)
    if config_path is None:
        return None
    try:
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
    except Exception as e:
        raise ConfigError(f"failed to parse {config_path}: {e}") from e

    project_root = config_path.parent.resolve()
    project_table = data.get("project") or {}
    name = project_table.get("name") if isinstance(project_table, dict) else None
    proof = _parse_proof(project_root, data)
    return ProjectConfig(
        project_root=project_root,
        config_path=config_path,
        name=name if isinstance(name, str) else None,
        proof=proof,
    )
