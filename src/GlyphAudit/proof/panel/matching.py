"""Pure helpers for matching a Glyphs master to a system-font family member.

Split out from `panel.py` so it can be unit-tested without importing AppKit
or driving Glyphs.app. Everything here is string-in / string-out.

Two responsibilities:

  1. `master_to_target(master_name)` — infer (CSS-style weight, italic?) from
     a Glyphs master name. Handles conventional names (Regular / Bold /
     Italic / Bold Italic), plus the wider palette (Thin / Book / Semibold
     / Black etc.) and quirky ones (Display / Text) by defaulting to
     regular weight.

  2. `score_member(master_name, face_name)` — how well does `face_name`
     (a system font's face variant, e.g. "Bold Italic") match `master_name`?
     Higher is better; the winning member gets picked. Ties break on
     specificity of the matched weight token.
"""

from __future__ import annotations

# Ordered longest-first so 'extralight' matches before 'light' when both
# tokens overlap. The width heuristic doesn't check for `light` inside
# `flight`-style false positives — a master named "Flight" is a design
# choice that's out of scope; caller can override.
_WEIGHT_TOKENS: tuple[tuple[str, int], ...] = (
    ("ultralight", 100),
    ("extralight", 200),
    ("hairline",   100),
    ("semibold",   600),
    ("demibold",   600),
    ("extrabold",  800),
    ("ultrabold",  800),
    ("thin",       100),
    ("light",      300),
    ("book",       400),
    ("normal",     400),
    ("regular",    400),
    ("medium",     500),
    ("bold",       700),
    ("black",      900),
    ("heavy",      900),
)

# What we call an italic — Glyphs uses both terms. `oblique` is worth
# recognising because some system fonts (Helvetica Oblique) still spell it
# that way; treating it as italic-equivalent for scoring purposes lets
# `Italic` masters pair with `Oblique` faces automatically.
_ITALIC_TOKENS: tuple[str, ...] = ("italic", "oblique")


def _lower(name: str) -> str:
    return name.strip().lower()


def infer_weight(name: str) -> int:
    """Return the CSS-style weight (100–900) implied by the tokens in
    `name`. Defaults to 400 (regular) when no weight token is present."""
    low = _lower(name)
    for token, weight in _WEIGHT_TOKENS:
        if token in low:
            return weight
    return 400


def infer_italic(name: str) -> bool:
    low = _lower(name)
    return any(tok in low for tok in _ITALIC_TOKENS)


def master_to_target(master_name: str) -> tuple[int, bool]:
    """Infer (weight, italic) from a Glyphs master name."""
    return infer_weight(master_name), infer_italic(master_name)


def score_member(master_name: str, face_name: str) -> int:
    """Score how well `face_name` matches `master_name`. Higher is better.

    Scoring is asymmetric to bias toward the italic axis: an italic
    mismatch is almost always the wrong pair even if the weight is right.
    A face without any recognised weight token (bare "Regular", or empty
    when the font's family-only face has no subfamily) gets a small
    positive score so the system loader still resolves *something* when
    the family only has one member.
    """
    m_weight, m_italic = master_to_target(master_name)
    f_weight, f_italic = master_to_target(face_name)

    score = 0
    # Italic parity — hard weight because comparing italic master against
    # non-italic reference is almost always misleading.
    if m_italic == f_italic:
        score += 100
    else:
        score -= 200

    # Weight proximity — 100 points for an exact match, decays with
    # distance so 500 (Medium) beats 700 (Bold) when the master is 400.
    delta = abs(m_weight - f_weight)
    if delta == 0:
        score += 100
    else:
        # 100 pts at delta=0, ~50 pts at delta=200, floor at 0 for delta≥400.
        score += max(0, 100 - delta // 2)

    # Small tie-break: face names that carry an *explicit* recognised
    # weight token beat "Regular" defaults. Prevents "System · Verdana"
    # (Regular default) from beating "Verdana Bold" when the master is
    # Bold and both would otherwise land at similar scores.
    fl = _lower(face_name)
    if any(tok in fl for tok, _ in _WEIGHT_TOKENS if tok not in ("regular", "normal", "book")):
        score += 10

    return score


def pick_best_member(master_name: str, members: list[tuple[str, str]]) -> tuple[str, str] | None:
    """Pick the (postscript_name, face_name) pair from `members` that best
    matches `master_name`. Returns None if `members` is empty. Ties go to
    the first member in input order (typically NSFontManager's declared
    order, which puts regular-adjacent styles first)."""
    if not members:
        return None
    best = None
    best_score = None
    for ps_name, face_name in members:
        s = score_member(master_name, face_name)
        if best_score is None or s > best_score:
            best_score = s
            best = (ps_name, face_name)
    return best
