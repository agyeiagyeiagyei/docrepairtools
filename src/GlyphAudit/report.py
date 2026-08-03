"""Markdown report writer for ComparisonResult(s)."""

from __future__ import annotations

from typing import Iterable

from .comparator import ComparisonResult, CodepointRow, VariantRow, InternalRow


def write_markdown(results: Iterable[ComparisonResult], path: str,
                   title: str = "Glyph Audit Report") -> None:
    lines: list[str] = [f"# {title}", ""]

    results_list = list(results)
    lines += _summary(results_list)

    for result in results_list:
        lines += _result_section(result)

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ---------------------------------------------------------------------------

def _summary(results: list[ComparisonResult]) -> list[str]:
    lines: list[str] = ["## Summary", ""]
    lines.append("| Pair | Tier 1 (codepoint) | Tier 2 (variant) | Tier 3 (internal) |")
    lines.append("|------|--------------------|------------------|-------------------|")
    for r in results:
        c = r.counts()
        t1 = c["tier1"]
        t2 = c["tier2"]
        t3 = c["tier3"]["internal"]
        t1_summary = (
            f"{t1['match']} match · {t1['mismatch']} mismatch · "
            f"{t1['missing-in-reference']} missing · {t1['no-advance']} n/a"
        )
        t2_summary = (
            f"{t2['match']} match · {t2['mismatch']} mismatch · "
            f"{t2['missing-in-reference']} missing · {t2['no-advance']} n/a"
        )
        lines.append(
            f"| {r.target_label} → {r.reference_label} | {t1_summary} | {t2_summary} | {t3} |"
        )
    lines.append("")
    return lines


def _result_section(result: ComparisonResult) -> list[str]:
    lines: list[str] = [
        f"## {result.target_label} → {result.reference_label}",
        "",
        f"Tolerance: ±{result.tolerance_units} unit(s).",
    ]
    if result.filter_label:
        lines.append(f"Target filter: **{result.filter_label}**.")
    lines.append("")
    lines += _tier1_section(result.codepoint_rows)
    lines += _tier2_section(result.variant_rows)
    lines += _tier3_section(result.internal_rows)
    return lines


# ---------------------------------------------------------------------------
# Tier 1
# ---------------------------------------------------------------------------

def _tier1_section(rows: list[CodepointRow]) -> list[str]:
    out = ["### Tier 1 — Codepoint match", ""]
    mismatches = [r for r in rows if r.status == "mismatch"]
    missing    = [r for r in rows if r.status == "missing-in-reference"]
    no_adv     = [r for r in rows if r.status == "no-advance"]
    matches    = sum(1 for r in rows if r.status == "match")

    out.append(
        f"- **{matches}** matched within tolerance"
        f" · **{len(mismatches)}** mismatched"
        f" · **{len(missing)}** missing in reference"
        f" · **{len(no_adv)}** lacking advance data"
    )
    out.append("")

    if mismatches:
        out += ["#### Mismatches", "",
                "| U+ | Char | Working name | Working adv | Reference name | Reference adv | Δ |",
                "|----|------|--------------|-------------|----------------|---------------|---|"]
        for r in sorted(mismatches, key=lambda r: -abs(r.delta or 0)):
            out.append(_codepoint_row(r))
        out.append("")

    if missing:
        out += [f"#### Missing in reference ({len(missing)})", ""]
        # Compact list — codepoint, char, target name
        groups = _group_by_block(r.codepoint for r in missing)
        for block, cps in groups.items():
            cps_set = set(cps)
            block_rows = [r for r in missing if r.codepoint in cps_set]
            out.append(f"**{block}** ({len(block_rows)})")
            out.append("")
            out.append("| U+ | Char | Working name |")
            out.append("|----|------|--------------|")
            for r in block_rows:
                ch = r.char.replace("|", "\\|")
                out.append(f"| U+{r.codepoint:04X} | {ch} | `{r.target_name}` |")
            out.append("")

    return out


def _codepoint_row(r: CodepointRow) -> str:
    ch = r.char.replace("|", "\\|")
    delta = f"{r.delta:+g}" if r.delta is not None else "—"
    return (
        f"| U+{r.codepoint:04X} | {ch} | `{r.target_name}` | "
        f"{r.target_advance if r.target_advance is not None else '—'} | "
        f"`{r.reference_name or '—'}` | "
        f"{r.reference_advance if r.reference_advance is not None else '—'} | "
        f"{delta} |"
    )


# ---------------------------------------------------------------------------
# Tier 2
# ---------------------------------------------------------------------------

def _tier2_section(rows: list[VariantRow]) -> list[str]:
    out = ["### Tier 2 — Feature variant match", ""]
    mismatches = [r for r in rows if r.status == "mismatch"]
    missing    = [r for r in rows if r.status == "missing-in-reference"]
    no_adv     = [r for r in rows if r.status == "no-advance"]
    matches    = sum(1 for r in rows if r.status == "match")

    out.append(
        f"- **{matches}** matched within tolerance"
        f" · **{len(mismatches)}** mismatched"
        f" · **{len(missing)}** missing in reference"
        f" · **{len(no_adv)}** lacking advance data"
    )
    out.append("")

    if mismatches:
        out += ["#### Mismatches", "",
                "| Feature | Base U+ | Char | Working name | Working adv | Reference name | Reference adv | Δ |",
                "|---------|---------|------|--------------|-------------|----------------|---------------|---|"]
        for r in sorted(mismatches, key=lambda r: (r.feature, -abs(r.delta or 0))):
            ch = r.base_char.replace("|", "\\|")
            delta = f"{r.delta:+g}" if r.delta is not None else "—"
            out.append(
                f"| `{r.feature}` | U+{r.base_codepoint:04X} | {ch} | `{r.target_name}` | "
                f"{r.target_advance if r.target_advance is not None else '—'} | "
                f"`{r.reference_name or '—'}` | "
                f"{r.reference_advance if r.reference_advance is not None else '—'} | "
                f"{delta} |"
            )
        out.append("")

    if missing:
        out += [f"#### Missing in reference ({len(missing)})", ""]
        out += ["| Feature | Base U+ | Char | Working name |",
                "|---------|---------|------|--------------|"]
        for r in sorted(missing, key=lambda r: (r.feature, r.base_codepoint)):
            ch = r.base_char.replace("|", "\\|")
            out.append(
                f"| `{r.feature}` | U+{r.base_codepoint:04X} | {ch} | `{r.target_name}` |"
            )
        out.append("")

    return out


# ---------------------------------------------------------------------------
# Tier 3
# ---------------------------------------------------------------------------

def _tier3_section(rows: list[InternalRow]) -> list[str]:
    if not rows:
        return ["### Tier 3 — Internal-only", "",
                "_No internal-only glyphs._", ""]
    by_note: dict[str, list[str]] = {}
    for r in rows:
        by_note.setdefault(r.note, []).append(r.glyph_name)

    out = ["### Tier 3 — Internal-only",
           "",
           f"{len(rows)} target glyph(s) with no codepoint and no recognised "
           "feature suffix — these have no logical counterpart in the reference and are listed for completeness.",
           ""]
    for note, names in sorted(by_note.items(), key=lambda kv: -len(kv[1])):
        out.append(f"**{note}** ({len(names)})")
        out.append("")
        out.append(", ".join(f"`{n}`" for n in names))
        out.append("")
    return out


# ---------------------------------------------------------------------------
# Unicode block grouping for missing-in-reference tables
# ---------------------------------------------------------------------------

_BLOCKS = [
    ("Basic Latin",                 0x0020, 0x007E),
    ("Latin-1 Supplement",          0x00A0, 0x00FF),
    ("Latin Extended-A",            0x0100, 0x017F),
    ("Latin Extended-B",            0x0180, 0x024F),
    ("IPA / Spacing Modifiers",     0x0250, 0x02FF),
    ("Combining Marks",             0x0300, 0x036F),
    ("Greek",                       0x0370, 0x03FF),
    ("Cyrillic",                    0x0400, 0x04FF),
    ("Cyrillic Supplement",         0x0500, 0x052F),
    ("Armenian",                    0x0530, 0x058F),
    ("General Punctuation",         0x2000, 0x206F),
    ("Currency Symbols",            0x20A0, 0x20CF),
    ("Letterlike / Number Forms",   0x2100, 0x218F),
    ("Misc Symbols / Box Drawing",  0x2190, 0x2BFF),
]


def _block_of(cp: int) -> str:
    for name, lo, hi in _BLOCKS:
        if lo <= cp <= hi:
            return name
    return "Other"


def _group_by_block(cps) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for cp in sorted(cps):
        out.setdefault(_block_of(cp), []).append(cp)
    return out
