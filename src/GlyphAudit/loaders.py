"""Font loaders: TTF/OTF, Glyphs source, and system-installed fonts.

Each loader returns a `FontView`. The CLI dispatches based on the
reference spec syntax:

    path/to/Helvetica.ttf            -> TTFLoader (static)
    path/to/Inter[wght].ttf@wght=400  -> TTFLoader, instantiated at wght=400
    path/to/MyTypeface.glyphspackage -> GlyphsLoader
    path/to/MyTypeface.glyphs        -> GlyphsLoader
    Helvetica-system                 -> SystemLoader (suffix '-system')
    Helvetica Bold-system            -> SystemLoader (space-form family+style)
    Helvetica-Bold-system            -> SystemLoader (dash-form family+style)
    Inter-system@wght=400          -> SystemLoader, VF instantiated at wght=400

System loading walks platform font directories looking for a face whose
family/subfamily names match the requested name (case-insensitive).
"""

from __future__ import annotations

import os
import platform
import re
import sys
from typing import Optional

from .model import FontView, FEATURE_SUFFIXES, parse_variant_suffix


SYSTEM_SUFFIX = "-system"


# ---------------------------------------------------------------------------
# Spec parsing
# ---------------------------------------------------------------------------

def _split_axes(spec: str) -> tuple[str, dict[str, float]]:
    """Strip an `@axis=value,axis=value` suffix off a spec, returning the
    cleaned spec plus a dict of axis tag -> float."""
    if "@" not in spec:
        return spec, {}
    head, _, tail = spec.rpartition("@")
    axes: dict[str, float] = {}
    for part in tail.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError(
                f"Bad axis token {part!r} in spec {spec!r}: expected NAME=VALUE."
            )
        name, _, raw = part.partition("=")
        name = name.strip()
        try:
            axes[name] = float(raw.strip())
        except ValueError as e:
            raise ValueError(
                f"Bad axis value {raw!r} for {name!r} in spec {spec!r}: {e}"
            ) from e
    return head, axes


# ---------------------------------------------------------------------------
# Public dispatch
# ---------------------------------------------------------------------------

def load_font(spec: str, *, master: Optional[str] = None,
              label: Optional[str] = None,
              axes: Optional[dict[str, float]] = None) -> FontView:
    """Load a font from `spec`. `master` selects a Glyphs-source master
    by name (case-insensitive substring match) when applicable.
    `label` overrides the auto-derived display label.
    `axes` pins a variable font's design-space location (merged with any
    `@axis=value` suffix in the spec, with the explicit `axes` param
    taking precedence on conflict)."""

    spec, spec_axes = _split_axes(spec)
    merged_axes: dict[str, float] = {**spec_axes, **(axes or {})}

    if spec.endswith(SYSTEM_SUFFIX):
        family = spec[:-len(SYSTEM_SUFFIX)].strip()
        return _load_system(family, label=label,
                            axes=merged_axes if merged_axes else None)

    if not os.path.exists(spec):
        raise FileNotFoundError(f"Font source not found: {spec}")

    lower = spec.lower()
    if lower.endswith((".ttf", ".otf")):
        return _load_ttf(spec, label=label,
                         axes=merged_axes if merged_axes else None)
    if lower.endswith((".glyphspackage", ".glyphs")):
        if merged_axes:
            raise ValueError(
                "Axis pinning (@axis=value) is not supported for Glyphs sources. "
                "Use a master selector via --pair instead."
            )
        return _load_glyphs(spec, master=master, label=label)

    raise ValueError(
        f"Unrecognised font source: {spec!r}. "
        f"Use a .ttf/.otf, .glyphspackage/.glyphs, or 'Name{SYSTEM_SUFFIX}'."
    )


# ---------------------------------------------------------------------------
# TTF / OTF loader (compiled font with a real GSUB table)
# ---------------------------------------------------------------------------

def _load_ttf(path: str, label: Optional[str] = None,
              axes: Optional[dict[str, float]] = None) -> FontView:
    from fontTools.ttLib import TTFont

    font = TTFont(path)

    if axes:
        if "fvar" not in font:
            raise ValueError(
                f"Cannot apply axis pin {axes!r} to {path!r}: it has no fvar "
                f"table (not a variable font)."
            )
        font = _instantiate_vf(font, axes, source_path=path)

    upm = font["head"].unitsPerEm

    cmap = dict(font.getBestCmap())  # cp -> glyphname
    hmtx = font["hmtx"]
    advances = {name: hmtx[name][0] for name in font.getGlyphOrder()}
    # LSB comes free from hmtx (second element of each entry). RSB has to
    # be derived: RSB = advance - LSB - (xMax - xMin). Empty glyphs
    # (space, zero-contour marks) get bbox=0 and land at RSB = advance - LSB.
    lsbs: dict[str, int] = {name: hmtx[name][1] for name in font.getGlyphOrder()}
    rsbs: dict[str, int] = {}
    glyf_table = font["glyf"] if "glyf" in font else None
    for name in font.getGlyphOrder():
        adv, lsb = hmtx[name]
        width = 0
        if glyf_table is not None:
            try:
                g = glyf_table[name]
                if g.numberOfContours != 0:
                    coords = g.getCoordinates(glyf_table)[0]
                    if len(coords):
                        xs = [c[0] for c in coords]
                        width = max(xs) - min(xs)
            except (AttributeError, KeyError):
                pass
        rsbs[name] = adv - lsb - width

    name_to_cp: dict[str, int] = {}
    for cp, name in cmap.items():
        name_to_cp.setdefault(name, cp)

    gsub_variants: dict[tuple[int, str], str] = {}
    if "GSUB" in font:
        gsub_variants = _extract_gsub_single_subs(
            font["GSUB"].table, name_to_cp,
            wanted_features=set(FEATURE_SUFFIXES.values()),
        )

    if label is None:
        try:
            family   = font["name"].getDebugName(1) or ""
            subfam   = font["name"].getDebugName(2) or ""
            label    = (f"{family} {subfam}").strip() or os.path.basename(path)
        except Exception:
            label = os.path.basename(path)
        if axes:
            label = f"{label} [{_axes_to_label(axes)}]"

    return FontView(
        label=label,
        source=f"{path}@{_axes_to_label(axes)}" if axes else path,
        source_kind="ttf",
        upm=upm,
        cmap=cmap,
        advances=advances,
        left_sidebearings=lsbs,
        right_sidebearings=rsbs,
        gsub_variants=gsub_variants,
        all_glyph_names=set(font.getGlyphOrder()),
    )


def _instantiate_vf(font, axes: dict[str, float], source_path: str):
    """Instantiate a variable font at the given axis location.
    Returns a new TTFont with hmtx / GSUB resolved at that point."""
    try:
        from fontTools.varLib.instancer import instantiateVariableFont
    except ImportError as e:
        raise RuntimeError(
            f"fontTools varLib.instancer is required to pin {source_path!r} "
            f"at {axes!r}. Upgrade fontTools."
        ) from e

    fvar_axes = {a.axisTag for a in font["fvar"].axes}
    unknown = set(axes) - fvar_axes
    if unknown:
        raise ValueError(
            f"Axis tag(s) {sorted(unknown)} not in {source_path!r}'s fvar "
            f"(available: {sorted(fvar_axes)})."
        )

    return instantiateVariableFont(font, axes, inplace=False)


def _axes_to_label(axes: dict[str, float]) -> str:
    return ",".join(f"{k}={_fmt_axis_value(v)}" for k, v in sorted(axes.items()))


def _fmt_axis_value(v: float) -> str:
    """Strip trailing .0 so '700.0' prints as '700' for nicer labels."""
    if v == int(v):
        return str(int(v))
    return f"{v:g}"


def _extract_gsub_single_subs(
    gsub_table,
    name_to_cp: dict[str, int],
    wanted_features: set[str],
) -> dict[tuple[int, str], str]:
    """Walk GSUB single-substitution lookups for `wanted_features` and
    return {(source_codepoint, feature_tag): variant_glyph_name}.

    Skips multi-substitution and contextual lookups for now — the
    suffix-based variants we care about (smcp/ss0X/onum/...) are almost
    always SingleSubst.
    """
    out: dict[tuple[int, str], str] = {}
    feature_list = gsub_table.FeatureList
    lookup_list = gsub_table.LookupList

    for fr in feature_list.FeatureRecord:
        tag = fr.FeatureTag
        if tag not in wanted_features:
            continue
        for li in fr.Feature.LookupListIndex:
            lookup = lookup_list.Lookup[li]
            for subtable in lookup.SubTable:
                mapping = getattr(subtable, "mapping", None)
                if mapping is None:
                    continue
                for src_name, dst_name in mapping.items():
                    cp = name_to_cp.get(src_name)
                    if cp is None:
                        continue
                    out.setdefault((cp, tag), dst_name)

    return out


# ---------------------------------------------------------------------------
# Glyphs source loader (.glyphspackage / .glyphs)
# ---------------------------------------------------------------------------

def _load_glyphs(path: str, *, master: Optional[str] = None,
                 label: Optional[str] = None) -> FontView:
    import glyphsLib

    font = glyphsLib.GSFont(path)
    upm  = font.upm

    if not font.masters:
        raise ValueError(f"No masters in {path}")

    chosen = _pick_master(font.masters, master)
    if chosen is None:
        master_names = ", ".join(repr(m.name) for m in font.masters)
        raise ValueError(
            f"Master {master!r} not found in {path}. Available: {master_names}"
        )

    cmap: dict[int, str] = {}
    advances: dict[str, int] = {}
    all_names: set[str] = set()
    name_to_cp: dict[str, int] = {}
    colors: dict[str, int] = {}

    for g in font.glyphs:
        all_names.add(g.name)
        # Encoded? Glyphs stores unicode as a hex string or list of hex strings.
        codepoints = _glyph_unicodes(g)
        for cp in codepoints:
            cmap.setdefault(cp, g.name)
            name_to_cp.setdefault(g.name, cp)

        # Advance for the chosen master, if a layer exists for it.
        for layer in g.layers:
            if layer.layerId == chosen.id:
                advances[g.name] = int(round(layer.width))
                break

        if g.color is not None:
            try:
                colors[g.name] = int(g.color)
            except (TypeError, ValueError):
                pass

    # Build feature variants from name suffixes
    gsub_variants: dict[tuple[int, str], str] = {}
    for name in all_names:
        parsed = parse_variant_suffix(name)
        if not parsed:
            continue
        base, feature = parsed
        cp = name_to_cp.get(base)
        if cp is not None:
            gsub_variants.setdefault((cp, feature), name)

    if label is None:
        family = font.familyName or os.path.splitext(os.path.basename(path))[0]
        label = f"{family} {chosen.name}".strip()

    return FontView(
        label=label,
        source=path,
        source_kind="glyphs",
        upm=upm,
        cmap=cmap,
        advances=advances,
        gsub_variants=gsub_variants,
        all_glyph_names=all_names,
        colors=colors,
    )


def _glyph_unicodes(g) -> list[int]:
    """Return all codepoints for a glyphsLib GSGlyph."""
    out: list[int] = []
    raw = getattr(g, "unicodes", None) or ([g.unicode] if g.unicode else [])
    for u in raw:
        if not u:
            continue
        try:
            out.append(int(str(u), 16))
        except ValueError:
            pass
    return out


def _pick_master(masters, requested: Optional[str]):
    if requested is None:
        return masters[0]
    rl = requested.lower()
    for m in masters:
        if (m.name or "").lower() == rl:
            return m
    for m in masters:
        if rl in (m.name or "").lower():
            return m
    return None


# ---------------------------------------------------------------------------
# System font loader
# ---------------------------------------------------------------------------

def _load_system(family: str, label: Optional[str] = None,
                 axes: Optional[dict[str, float]] = None) -> FontView:
    """Find a system-installed font by family name and load it as TTF.

    Walks platform-conventional font directories; matches the family
    name (and optional subfamily, if the request includes a space) by
    reading each candidate's `name` table. If `axes` is given, the
    chosen file must be a variable font and is instantiated at that
    location.
    """
    if not family:
        raise ValueError("Empty family name for system font lookup.")

    target_family, target_subfamily = _split_family_subfamily(family)
    candidates = _collect_system_font_paths()
    if not candidates:
        raise RuntimeError(
            f"No system font directories found on platform {platform.system()!r}"
        )

    from fontTools.ttLib import TTFont

    best_match_path = None
    best_match_score = -1

    for path in candidates:
        try:
            font = TTFont(path, lazy=True)
        except Exception:
            continue
        try:
            fam_name = font["name"].getDebugName(1) or ""
            sub_name = font["name"].getDebugName(2) or ""
        except Exception:
            font.close()
            continue
        finally:
            font.close()

        score = _score_family_match(target_family, target_subfamily,
                                    fam_name, sub_name)
        if score > best_match_score:
            best_match_score = score
            best_match_path = path

    if best_match_path is None or best_match_score < 1:
        raise FileNotFoundError(
            f"Could not locate system font matching {family!r}. "
            f"Try the exact family name, optionally with a subfamily "
            f"(e.g. 'Helvetica Bold' or 'Helvetica-Bold')."
        )

    view = _load_ttf(best_match_path, label=label or family, axes=axes)
    # Mark the source kind so the report can show provenance correctly
    suffix = f"@{_axes_to_label(axes)}" if axes else ""
    return FontView(
        label=view.label,
        source=f"{family}{suffix} ({best_match_path})",
        source_kind="system",
        upm=view.upm,
        cmap=view.cmap,
        advances=view.advances,
        gsub_variants=view.gsub_variants,
        all_glyph_names=view.all_glyph_names,
    )


def _split_family_subfamily(name: str) -> tuple[str, Optional[str]]:
    """Heuristic split: 'Helvetica Bold' -> ('Helvetica', 'Bold').

    Accepts either spaces or hyphens between family and subfamily, so
    'Helvetica-Bold' parses the same as 'Helvetica Bold'. Only the LAST
    separator before a recognised style token is treated as the split
    point — multi-word families like 'Source Sans Pro' or
    'Source-Sans-Pro' are kept intact.

    Common subfamily tokens are recognised at the end; everything else
    is treated as the family name.
    """
    SUBFAM_TOKENS = {
        "regular", "bold", "italic", "oblique", "thin", "light",
        "medium", "semibold", "extrabold", "black", "condensed",
        "bolditalic", "bold italic",
    }
    lower = name.lower()
    for token in sorted(SUBFAM_TOKENS, key=len, reverse=True):
        for sep in (" ", "-"):
            needle = sep + token
            if lower.endswith(needle):
                family = name[: -len(needle)].strip().replace("-", " ").strip()
                subfam = name[-len(token):].strip()
                return family, subfam
    # No recognised subfamily — normalise hyphens to spaces so dash-form
    # families ('Source-Sans-Pro') match a name table whose family is the
    # space form.
    return name.replace("-", " ").strip(), None


def _score_family_match(target_family: str, target_subfamily: Optional[str],
                        candidate_family: str, candidate_subfamily: str) -> int:
    """Return >0 for any match, 2 for exact family + (subfamily if requested)."""
    if not candidate_family:
        return -1
    cf = candidate_family.lower().strip()
    cs = (candidate_subfamily or "").lower().strip()
    tf = target_family.lower().strip()
    ts = (target_subfamily or "").lower().strip()

    if tf != cf:
        return -1
    if ts:
        return 2 if ts == cs else 0
    # No subfamily requested: prefer Regular over others
    return 2 if cs in ("regular", "", "roman") else 1


def _collect_system_font_paths() -> list[str]:
    sys_name = platform.system()
    dirs: list[str] = []

    if sys_name == "Darwin":
        dirs = [
            "/System/Library/Fonts",
            "/System/Library/Fonts/Supplemental",
            "/Library/Fonts",
            os.path.expanduser("~/Library/Fonts"),
        ]
    elif sys_name == "Linux":
        dirs = [
            "/usr/share/fonts",
            "/usr/local/share/fonts",
            os.path.expanduser("~/.fonts"),
            os.path.expanduser("~/.local/share/fonts"),
        ]
    elif sys_name == "Windows":
        win_dir = os.environ.get("WINDIR", r"C:\Windows")
        dirs = [
            os.path.join(win_dir, "Fonts"),
            os.path.expanduser(r"~\AppData\Local\Microsoft\Windows\Fonts"),
        ]

    out: list[str] = []
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for root, _, files in os.walk(d):
            for f in files:
                if f.lower().endswith((".ttf", ".otf", ".ttc")):
                    out.append(os.path.join(root, f))
    return out
