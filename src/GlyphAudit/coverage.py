"""Coverage gap analysis: is everything in the reference present in the target?

The audit comparator (`comparator.py`) asks "do my target glyphs match the
reference?". This module asks the inverse — "is anything the reference
covers missing from my target?" For document-replacement fonts the answer
must be "no": any codepoint or GSUB-reachable variant present in the
reference but absent from the replacement renders with a fallback font in
real documents.

Two gap classes are distinguished from true absences because they are
cheap fixes, not missing work:

- ``unencoded-in-target`` — the glyph exists in the target but has no
  Unicode value (documents can't reach it; fix is one field in Glyphs).
- ``present-unlinked`` — a variant glyph exists but isn't reachable via
  the target's feature wiring / recognised suffix.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import Optional

from fontTools.unicodedata import block as _unicode_block

from .model import FontView

ABSENT = "absent"
UNENCODED_IN_TARGET = "unencoded-in-target"
PRESENT_UNLINKED = "present-unlinked"


# ---------------------------------------------------------------------------
# Compiled-GSUB access (feature matching + feature copying)
# ---------------------------------------------------------------------------

def _ttfont_for_view(view: FontView):
    """fontTools TTFont for a loaded FontView, or None when the underlying
    file can't be opened (Glyphs sources, unsaved fonts). Strips the
    `@axis=…` pin suffix and the system-font wrapper from `view.source`.
    Feature rules don't change under instancing, so axis pins are ignored.
    """
    import os
    src = getattr(view, "source", "") or ""
    kind = getattr(view, "source_kind", "")
    if kind == "system":
        if "(" in src and src.endswith(")"):
            src = src.rsplit("(", 1)[-1].rstrip(")")
        else:
            return None
    else:
        src = src.split("@", 1)[0]
    if not src or not os.path.isfile(src):
        return None
    try:
        from fontTools.ttLib import TTFont
    except ImportError:
        return None
    try:
        return TTFont(src, lazy=True)
    except Exception:
        return None


def gsub_features_for_view(view: FontView):
    """{feature_tag: {source_glyph: substitute_glyph}} — union of all
    single-substitution rules across scripts/lookups, plus ligature rules
    under the special key structure of `gsub_ligatures_for_view`.
    Returns None when no compiled GSUB is reachable."""
    tt = _ttfont_for_view(view)
    if tt is None or "GSUB" not in tt:
        return None
    table = tt["GSUB"].table
    out: dict[str, dict[str, str]] = {}
    for rec in table.FeatureList.FeatureRecord:
        mapping = out.setdefault(rec.FeatureTag, {})
        for idx in rec.Feature.LookupListIndex:
            lookup = table.LookupList.Lookup[idx]
            for sub in lookup.SubTable:
                if lookup.LookupType == 1:  # SingleSubst
                    mapping.update(sub.mapping)
    tt.close()
    return out


def gsub_ligatures_for_view(view: FontView):
    """{feature_tag: [(components_tuple, ligature_glyph), ...]} — union of
    ligature substitutions. Returns None when unreachable."""
    tt = _ttfont_for_view(view)
    if tt is None or "GSUB" not in tt:
        return None
    table = tt["GSUB"].table
    out: dict[str, list] = {}
    for rec in table.FeatureList.FeatureRecord:
        ligs = out.setdefault(rec.FeatureTag, [])
        seen = set(ligs)
        for idx in rec.Feature.LookupListIndex:
            lookup = table.LookupList.Lookup[idx]
            if lookup.LookupType != 4:  # LigatureSubst
                continue
            for sub in lookup.SubTable:
                for first, lig_list in sub.ligatures.items():
                    for lig in lig_list:
                        key = ((first, *lig.Component), lig.LigGlyph)
                        if key not in seen:
                            seen.add(key)
                            ligs.append(key)
    tt.close()
    return out


@dataclass
class CodepointGap:
    codepoint: int
    char: str
    unicode_name: str
    ref_glyph: str
    kind: str  # ABSENT | UNENCODED_IN_TARGET

    @property
    def block(self) -> str:
        return _unicode_block(self.codepoint)


@dataclass
class VariantGap:
    base_codepoint: int
    base_char: str
    feature: str
    ref_glyph: str
    kind: str  # ABSENT | PRESENT_UNLINKED


@dataclass
class CoverageResult:
    pair_label: str
    target_label: str
    reference_label: str
    reference_codepoints: int
    reference_variants: int
    codepoint_gaps: list[CodepointGap] = field(default_factory=list)
    variant_gaps: list[VariantGap] = field(default_factory=list)
    feature_rows: list[FeatureRow] = field(default_factory=list)
    cp_matrix: list[MatrixRow] = field(default_factory=list)
    var_matrix: list[MatrixRow] = field(default_factory=list)
    fea_summary: Optional[str] = None
    reverse: Optional["CoverageResult"] = None
    """Gaps in the opposite direction (what the target covers that the
    reference lacks). Informational — never affects absent_count()."""

    def absent_count(self) -> int:
        return (
            sum(1 for g in self.codepoint_gaps if g.kind == ABSENT)
            + sum(1 for g in self.variant_gaps if g.kind == ABSENT)
        )


def _char(cp: int) -> str:
    ch = chr(cp)
    return ch if ch.isprintable() else "·"


def coverage_gaps(
    target_view: FontView,
    reference_view: FontView,
    pair_label: str = "",
) -> CoverageResult:
    """Everything the reference covers that the target lacks.

    Codepoints: reference cmap minus target cmap. A missing codepoint
    whose reference glyph NAME exists in the target is classified
    `unencoded-in-target` rather than `absent`.

    Variants: reference GSUB map (compiled GSUB for binaries, suffix
    conventions for Glyphs sources) minus the target's. A missing variant
    whose reference glyph NAME exists in the target is classified
    `present-unlinked`.
    """
    result = CoverageResult(
        pair_label=pair_label,
        target_label=target_view.label,
        reference_label=reference_view.label,
        reference_codepoints=len(reference_view.cmap),
        reference_variants=len(reference_view.gsub_variants),
    )

    target_names = target_view.all_glyph_names
    for cp in sorted(reference_view.cmap):
        if cp in target_view.cmap:
            continue
        ref_glyph = reference_view.cmap[cp]
        kind = UNENCODED_IN_TARGET if ref_glyph in target_names else ABSENT
        result.codepoint_gaps.append(CodepointGap(
            codepoint=cp,
            char=_char(cp),
            unicode_name=unicodedata.name(chr(cp), "<unnamed>"),
            ref_glyph=ref_glyph,
            kind=kind,
        ))

    for (cp, feature) in sorted(reference_view.gsub_variants):
        if (cp, feature) in target_view.gsub_variants:
            continue
        ref_glyph = reference_view.gsub_variants[(cp, feature)]
        kind = PRESENT_UNLINKED if ref_glyph in target_names else ABSENT
        result.variant_gaps.append(VariantGap(
            base_codepoint=cp,
            base_char=_char(cp),
            feature=feature,
            ref_glyph=ref_glyph,
            kind=kind,
        ))

    return result


def reverse_gaps(
    target_view: FontView,
    reference_view: FontView,
    pair_label: str = "",
) -> CoverageResult:
    """Gaps in the opposite direction: what the TARGET covers that the
    reference lacks. Computed by running the same core with the roles
    swapped, then relabeling so `target_label` / `reference_label` match
    the forward result's framing.
    """
    result = coverage_gaps(reference_view, target_view, pair_label=pair_label)
    result.target_label = target_view.label
    result.reference_label = reference_view.label
    return result


# ---------------------------------------------------------------------------
# Feature matching
# ---------------------------------------------------------------------------

@dataclass
class FeatureRow:
    feature: str
    ref_rules: int
    covered: int     # rules the target can serve (variant present + linked)
    status: str      # "full" | "partial" | "missing"


def feature_table(target_view: FontView, reference_view: FontView) -> list[FeatureRow]:
    """Per-feature match between reference and target.

    Rule counts come from the reference's compiled GSUB when reachable
    (includes features the suffix map can't see, like dlig/locl); target
    coverage is measured on the (codepoint, feature) variant map.
    """
    ref_gsub = gsub_features_for_view(reference_view) or {}
    ref_ligs = gsub_ligatures_for_view(reference_view) or {}
    tags = sorted(
        set(ref_gsub) | set(ref_ligs)
        | {f for _cp, f in reference_view.gsub_variants}
        | {f for _cp, f in target_view.gsub_variants}
    )
    rows: list[FeatureRow] = []
    for tag in tags:
        ref_rules = len(ref_gsub.get(tag, {})) + len(ref_ligs.get(tag, []))
        if not ref_rules:
            ref_rules = sum(1 for _cp, f in reference_view.gsub_variants if f == tag)
        covered = sum(
            1 for (cp, f) in reference_view.gsub_variants
            if f == tag and (cp, f) in target_view.gsub_variants
        )
        status = "full" if covered >= ref_rules and ref_rules else (
            "partial" if covered else "missing"
        )
        rows.append(FeatureRow(feature=tag, ref_rules=ref_rules,
                               covered=covered, status=status))
    return rows


# ---------------------------------------------------------------------------
# Glyph matrix (yes/no per side)
# ---------------------------------------------------------------------------

@dataclass
class MatrixRow:
    label: str
    target_name: Optional[str]
    ref_name: Optional[str]


def glyph_matrix(target_view: FontView, reference_view: FontView):
    """(codepoint_rows, variant_rows) over the UNION of both fonts —
    the full yes/no presence matrix."""
    cp_rows = [
        MatrixRow(
            label=f"{_char(cp)} (U+{cp:04X})",
            target_name=target_view.cmap.get(cp),
            ref_name=reference_view.cmap.get(cp),
        )
        for cp in sorted(set(target_view.cmap) | set(reference_view.cmap))
    ]
    keys = sorted(set(target_view.gsub_variants) | set(reference_view.gsub_variants))
    var_rows = [
        MatrixRow(
            label=f"{_char(cp)} · {feature}",
            target_name=target_view.gsub_variants.get((cp, feature)),
            ref_name=reference_view.gsub_variants.get((cp, feature)),
        )
        for cp, feature in keys
    ]
    return cp_rows, var_rows


# ---------------------------------------------------------------------------
# Feature copying (GSUB → .fea in target glyph names)
# ---------------------------------------------------------------------------

def build_feature_file(
    target_view: FontView,
    reference_view: FontView,
    gsub_map: dict | None = None,
    lig_map: dict | None = None,
) -> tuple[str, dict]:
    """Decompile the reference's GSUB into AFDKO .fea text whose rules
    reference TARGET glyph names (resolved by codepoint through the
    target's cmap / variant map — names alone don't transfer).

    Returns (fea_text, stats). Rules whose glyphs are missing from the
    target are skipped and counted — those are the coverage gaps.
    """
    if gsub_map is None:
        gsub_map = gsub_features_for_view(reference_view) or {}
    if lig_map is None:
        lig_map = gsub_ligatures_for_view(reference_view) or {}

    ref_name_to_cp: dict[str, int] = {}
    for cp, name in reference_view.cmap.items():
        ref_name_to_cp.setdefault(name, cp)

    lines = [
        f"# OpenType features copied from: {reference_view.label}",
        f"# Rewritten into target glyph names of: {target_view.label}",
        "# Rules referencing glyphs missing from the target were skipped",
        "# (they are the coverage gaps — add those glyphs first).",
        "",
    ]
    stats = {"features": 0, "rules": 0, "skipped": 0, "skipped_features": []}

    for tag in sorted(set(gsub_map) | set(lig_map)):
        rules: list[str] = []
        skipped = 0
        for src, dst in sorted(gsub_map.get(tag, {}).items()):
            cp_src = ref_name_to_cp.get(src)
            t_src = target_view.cmap.get(cp_src) if cp_src is not None else None
            t_dst = None
            if cp_src is not None:
                t_dst = target_view.gsub_variants.get((cp_src, tag))
            if t_dst is None:
                cp_dst = ref_name_to_cp.get(dst)
                t_dst = target_view.cmap.get(cp_dst) if cp_dst is not None else None
            if t_dst is None and dst in target_view.all_glyph_names:
                t_dst = dst
            if not t_src or not t_dst:
                skipped += 1
                continue
            rules.append(f"    sub {t_src} by {t_dst};")
        for components, lig in lig_map.get(tag, []):
            t_components = []
            for comp in components:
                cp = ref_name_to_cp.get(comp)
                t_name = target_view.cmap.get(cp) if cp is not None else None
                if t_name is None and comp in target_view.all_glyph_names:
                    t_name = comp
                t_components.append(t_name)
            t_lig = lig if lig in target_view.all_glyph_names else None
            if any(c is None for c in t_components) or t_lig is None:
                skipped += 1
                continue
            rules.append(f"    sub {' '.join(t_components)} by {t_lig};")
        if not rules:
            if skipped:
                stats["skipped"] += skipped
                stats["skipped_features"].append(tag)
            continue
        stats["features"] += 1
        stats["rules"] += len(rules)
        stats["skipped"] += skipped
        header = f"feature {tag} {{"
        lines.append(header)
        lines.append(f"    # {len(rules)} rules from reference"
                     + (f", {skipped} skipped (missing glyphs)" if skipped else ""))
        lines.extend(rules)
        lines.append(f"}} {tag};")
        lines.append("")

    return "\n".join(lines) + "\n", stats


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------

_KIND_LABELS = {
    ABSENT: "**absent**",
    UNENCODED_IN_TARGET: "unencoded in target",
    PRESENT_UNLINKED: "present, not feature-linked",
}


def _gap_table_lines(r: CoverageResult) -> list[str]:
    """The codepoint + variant gap tables for one direction."""
    lines: list[str] = []
    if r.codepoint_gaps:
        lines += ["### Missing codepoints", ""]
        by_block: dict[str, list[CodepointGap]] = {}
        for g in r.codepoint_gaps:
            by_block.setdefault(g.block, []).append(g)
        for block_name, gaps in sorted(by_block.items(), key=lambda kv: -len(kv[1])):
            lines += [
                f"#### {block_name} — {len(gaps)}",
                "",
                "| Codepoint | Char | Unicode name | Reference glyph | Status |",
                "|---|---|---|---|---|",
            ]
            for g in gaps:
                lines.append(
                    f"| U+{g.codepoint:04X} | {g.char} | {g.unicode_name} "
                    f"| {g.ref_glyph} | {_KIND_LABELS[g.kind]} |"
                )
            lines.append("")

    if r.variant_gaps:
        lines += ["### Missing feature variants", ""]
        by_feature: dict[str, list[VariantGap]] = {}
        for g in r.variant_gaps:
            by_feature.setdefault(g.feature, []).append(g)
        for feature, gaps in sorted(by_feature.items(), key=lambda kv: -len(kv[1])):
            lines += [
                f"#### {feature} — {len(gaps)}",
                "",
                "| Base | Feature | Reference glyph | Status |",
                "|---|---|---|---|",
            ]
            for g in gaps:
                lines.append(
                    f"| {g.base_char} (U+{g.base_codepoint:04X}) | {g.feature} "
                    f"| {g.ref_glyph} | {_KIND_LABELS[g.kind]} |"
                )
            lines.append("")
    return lines


def render_markdown(results: list[CoverageResult], title: str = "Coverage Gap Report",
                    skipped: list[str] | None = None) -> str:
    lines: list[str] = [f"# {title}", ""]
    if skipped:
        lines += ["**Skipped pairs** (master not found in target source):", ""]
        lines += [f"- {s}" for s in skipped]
        lines.append("")
    for r in results:
        cp_absent = sum(1 for g in r.codepoint_gaps if g.kind == ABSENT)
        cp_unenc = len(r.codepoint_gaps) - cp_absent
        var_absent = sum(1 for g in r.variant_gaps if g.kind == ABSENT)
        var_unlinked = len(r.variant_gaps) - var_absent
        status = "PASS" if r.absent_count() == 0 else "FAIL"
        lines += [
            f"## {r.pair_label} — {r.target_label} vs {r.reference_label}",
            "",
            f"**{status}** — reference covers {r.reference_codepoints} codepoints + "
            f"{r.reference_variants} feature variants. "
            f"Gaps: {len(r.codepoint_gaps)} codepoints "
            f"({cp_absent} absent, {cp_unenc} unencoded-in-target), "
            f"{len(r.variant_gaps)} variants "
            f"({var_absent} absent, {var_unlinked} present-unlinked).",
            "",
        ]

        lines += _gap_table_lines(r)

        if r.feature_rows:
            lines += [
                "### Feature matching",
                "",
                "| Feature | Ref rules | Covered in target | Status |",
                "|---|---|---|---|",
            ]
            for fr in r.feature_rows:
                lines.append(
                    f"| {fr.feature} | {fr.ref_rules} | {fr.covered} | {fr.status} |"
                )
            lines.append("")

        if r.fea_summary:
            lines += ["### Feature copy", "", r.fea_summary, ""]

        if not r.codepoint_gaps and not r.variant_gaps:
            lines += ["No gaps — target covers everything the reference covers.", ""]

        if r.reverse is not None:
            rev = r.reverse
            rev_cp_absent = sum(1 for g in rev.codepoint_gaps if g.kind == ABSENT)
            rev_var_absent = sum(1 for g in rev.variant_gaps if g.kind == ABSENT)
            lines += [
                f"### Reverse — in {r.target_label} but not in {r.reference_label}",
                "",
                f"Informational (does not affect PASS/FAIL): "
                f"{len(rev.codepoint_gaps)} codepoints ({rev_cp_absent} absent), "
                f"{len(rev.variant_gaps)} variants ({rev_var_absent} absent).",
                "",
            ]
            lines += _gap_table_lines(rev)
            if not rev.codepoint_gaps and not rev.variant_gaps:
                lines += ["Nothing — the reference covers everything the target covers.", ""]

        if r.cp_matrix:
            lines += [
                "### Full matrix — codepoints",
                "",
                f"| Glyph | {r.target_label} | {r.reference_label} |",
                "|---|---|---|",
            ]
            for m in r.cp_matrix:
                lines.append(
                    f"| {m.label} | {'yes' if m.target_name else 'no'} "
                    f"| {'yes' if m.ref_name else 'no'} |"
                )
            lines.append("")

        if r.var_matrix:
            lines += [
                "### Full matrix — feature variants",
                "",
                f"| Variant | {r.target_label} | {r.reference_label} |",
                "|---|---|---|",
            ]
            for m in r.var_matrix:
                lines.append(
                    f"| {m.label} | {'yes' if m.target_name else 'no'} "
                    f"| {'yes' if m.ref_name else 'no'} |"
                )
            lines.append("")

    return "\n".join(lines) + "\n"


def write_markdown(results: list[CoverageResult], output_path: str,
                   title: str = "Coverage Gap Report",
                   skipped: list[str] | None = None) -> None:
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(render_markdown(results, title=title, skipped=skipped))
