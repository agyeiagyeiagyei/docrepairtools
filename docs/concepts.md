# Concepts

GlyphAudit compares one **target** font against one or more **references**, per master, in three tiers. Every glyph in the target font is accounted for — even ones that don't have a Unicode codepoint.

## The three tiers

### Tier 1 — Codepoint match

Every encoded glyph in the target gets paired with the reference's glyph at the same Unicode codepoint. Their advance widths are compared.

Used to verify metrics-compatibility for the bulk of the character set.

### Tier 2 — Feature variant match

Glyphs whose name ends in a recognised OpenType feature suffix get paired by `(source codepoint, feature tag)`:

- `a.smcp` → reference's `smcp` substitution for U+0061
- `I.ss01` → reference's `ss01` substitution for U+0049
- `eight.dnom` → reference's `dnom` substitution for U+0038

Recognised suffixes: `smcp`, `c2sc`, `ss01–ss10`, `onum`, `pnum`, `lnum`, `tnum`, `numr`, `dnom`, `sinf`, `sups`, `ordn`, `salt`, `frac`, plus a few Glyphs-specific aliases (`osf`, `tosf`, `frac0`).

The reference's variant is found via its compiled GSUB table (TTF / OTF / system) or its own naming convention (Glyphs source).

### Tier 3 — Internal-only

Target glyphs that have no codepoint and no recognised feature suffix — components like `brevecomb_acutecomb`, ligature pieces like `f_f_i`, locl variants, font-internal helpers. Listed for completeness; no attempt to match against the reference.

## Row statuses

Within Tier 1 and Tier 2, every row is tagged:

| Status | Meaning |
|---|---|
| `match` | Within tolerance |
| `mismatch` | Advance differs by more than `--tolerance` |
| `missing-in-reference` | Codepoint or feature variant doesn't exist in the reference |
| `no-advance` | Target glyph has no advance width data |

`missing-in-reference` is *not* an error — it usually means the target font's coverage extends beyond the reference's set, which is expected.

## Coverage gaps (`glyph-audit coverage`)

The audit walks target → reference; coverage walks the other way: **what does the reference cover that the target lacks?** — the question a document-replacement font must answer with "nothing", since any missing codepoint or GSUB-reachable variant renders with a fallback font in real documents.

Two kinds of coverage are checked, in both directions:

- **Codepoints** — the reference cmap minus the target cmap.
- **Feature variants** — every variant reachable through the reference's GSUB (compiled GSUB for binaries, name suffixes for Glyphs sources).

Every gap is classified, because the fix differs:

| Status | Meaning | Fix |
|---|---|---|
| `absent` | Not in the target at all — fails the run (exit 1) | Draw/create the glyph |
| `unencoded-in-target` | The glyph exists but has no Unicode value | Assign the codepoint |
| `present, not feature-linked` | A variant glyph exists but isn't wired to the feature | Wire up the feature |

The report adds a **feature matching table** (per feature tag: reference rules vs rules the target can serve — `full` / `partial` / `missing`) and a **yes/no matrix** over the union of both fonts. `--emit-features` writes the reference's GSUB as `.fea` files rewritten into target glyph names; rules referencing missing glyphs are skipped.

## Reading the report

Each target/reference pairing produces one section. Each section starts with counts, then lists mismatches sorted by severity (largest delta first). Missing-in-reference lists are grouped by Unicode block so the scope of the gap is visible at a glance. Tier 3 entries are bucketed by note so component / ligature noise stays separated from real bugs (e.g. variants whose base glyph is missing).
