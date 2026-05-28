#!/usr/bin/env python3
"""
Build the artifacts the GlyphAudit Preview Vite app reads:

  * proof-font.ttf            – subset font built from --source, only glyphs
                                marked yellow / light-green in Glyphs (=
                                ready for proofing) plus essentials.
  * proof-config.json         – runtime config the React app fetches:
                                project name, proof font family, list of
                                reference fonts (copied under public/ref/).
  * available-chars.json      – codepoints in the proof font (drives the
                                "missing glyph" underline in the proof panel).
  * available-features.json   – per-feature compile status (drives the
                                Features dropdown).

All artifacts land in `public/` alongside this script, where Vite serves
them at the root URL. Run from anywhere; output paths are anchored to
the script's location, not the caller's CWD.

Examples (executed from a typeface project's root):

    # Minimal: project name comes from the source filename stem,
    # proof family becomes "<Name> Proof", references read from any
    # [instances.*] entries in ~/.glyph-audit/config.toml.
    python /path/to/GlyphAudit/preview/build.py \\
        --source sources/MyTypeface.glyphspackage

    # Explicit reference fonts (repeatable):
    python /path/to/GlyphAudit/preview/build.py \\
        --source sources/MyTypeface.glyphspackage \\
        --name "MyTypeface" \\
        --reference Verdana:regular=sources/reference/VERDANA.TTF \\
        --reference Verdana:bold=sources/reference/VERDANAB.TTF

    # Watch the source and rebuild on every .glyph save:
    python /path/to/GlyphAudit/preview/build.py \\
        --source sources/MyTypeface.glyphspackage --watch
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Output anchored to this script's location so the Vite app at preview/ and
# the build artifacts at preview/public/ always agree, regardless of where
# the user runs the script from.
PREVIEW_DIR    = Path(__file__).resolve().parent
OUTPUT_DIR     = PREVIEW_DIR / "public"
OUTPUT_PATH    = OUTPUT_DIR / "proof-font.ttf"
REF_OUTPUT_DIR = OUTPUT_DIR / "ref"
CONFIG_OUTPUT  = OUTPUT_DIR / "proof-config.json"
USER_CONFIG    = Path.home() / ".glyph-audit" / "config.toml"

# Glyphs that must always be included regardless of color
ESSENTIAL_GLYPHS = {"_notdef", "space"}

# GlyphsApp color indices that qualify for proofing
# 3 = Yellow (ready for testing), 4 = Light green (passed inspection)
PROOF_COLORS = {"3", "4"}

# Human-readable names for OpenType feature tags. Used to label rows in the
# preview app's Features dropdown. Anything not listed falls back to the tag
# itself, so unknown / project-specific tags still appear.
FEATURE_NAMES = {
    "aalt": "Access All Alternates",
    "calt": "Contextual Alternates",
    "case": "Case-Sensitive Forms",
    "ccmp": "Glyph Composition / Decomposition",
    "clig": "Contextual Ligatures",
    "cv01": "Character Variant 1", "cv02": "Character Variant 2",
    "dlig": "Discretionary Ligatures",
    "dnom": "Denominators",
    "frac": "Fractions",
    "hist": "Historical Forms",
    "hlig": "Historical Ligatures",
    "kern": "Kerning",
    "liga": "Standard Ligatures",
    "lnum": "Lining Figures",
    "locl": "Localized Forms",
    "numr": "Numerators",
    "onum": "Oldstyle Figures",
    "ordn": "Ordinals",
    "pnum": "Proportional Figures",
    "rlig": "Required Ligatures",
    "rtlm": "Right-to-Left Mirrored Forms",
    "salt": "Stylistic Alternates",
    "sinf": "Scientific Inferiors",
    "smcp": "Small Capitals",
    "ss01": "Stylistic Set 1", "ss02": "Stylistic Set 2",
    "subs": "Subscript",
    "sups": "Superscript",
    "swsh": "Swash",
    "titl": "Titling",
    "tnum": "Tabular Figures",
    "zero": "Slashed Zero",
}

# .fea / Glyphs feature-syntax tokens that look like glyph names but aren't.
# Anything not in source_glyphs is filtered out anyway, but excluding these up front
# keeps the missing-glyph diagnostics clean if we ever log unresolved tokens.
_FEA_KEYWORDS = {
    "sub", "by", "from", "lookup", "feature", "script", "language", "lookupflag",
    "IgnoreMarks", "UseMarkFilteringSet", "RightToLeft", "IgnoreBaseGlyphs",
    "IgnoreLigatures", "MarkAttachmentType", "pos", "position", "enum",
    "ignore", "reversesub", "rsub", "subtable", "include", "useExtension",
    "exclude_dflt", "anchor", "anchorDef", "valueRecordDef",
}


def _walk_array_dicts(text, key):
    """Yield each {…} entry's body inside `key = ( … );` in `text`.

    Walks character-by-character respecting double-quoted strings (with backslash
    escapes) and nested ()/[]/{} so embedded code strings don't confuse depth counting.
    """
    m = re.search(rf"^{re.escape(key)}\s*=\s*\(", text, re.MULTILINE)
    if not m:
        return
    pos = m.end()
    n = len(text)
    paren_depth = 1
    brace_depth = 0
    entry_start = -1
    in_string = False
    while pos < n and paren_depth > 0:
        ch = text[pos]
        if in_string:
            if ch == "\\" and pos + 1 < n:
                pos += 2
                continue
            if ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "(":
                paren_depth += 1
            elif ch == ")":
                paren_depth -= 1
            elif ch == "{":
                if brace_depth == 0 and paren_depth == 1:
                    entry_start = pos
                brace_depth += 1
            elif ch == "}":
                brace_depth -= 1
                if brace_depth == 0 and paren_depth == 1 and entry_start >= 0:
                    yield text[entry_start + 1 : pos]
                    entry_start = -1
        pos += 1


def _parse_entry_dict(body):
    """Parse a single `key = value;` body into a dict.

    Values: quoted strings (with `\\"` and `\\\\` un-escaped), arrays kept as raw text,
    everything else trimmed unquoted. Comments stripped.
    """
    out = {}
    pos = 0
    n = len(body)
    while pos < n:
        # skip whitespace and commas
        while pos < n and body[pos] in " \t\n\r,":
            pos += 1
        if pos >= n:
            break
        if body[pos] == "#":
            while pos < n and body[pos] != "\n":
                pos += 1
            continue
        kstart = pos
        while pos < n and (body[pos].isalnum() or body[pos] in "_.-"):
            pos += 1
        key = body[kstart:pos]
        while pos < n and body[pos] in " \t\n\r":
            pos += 1
        if pos < n and body[pos] == "=":
            pos += 1
            while pos < n and body[pos] in " \t\n\r":
                pos += 1
        if pos >= n:
            break
        if body[pos] == '"':
            pos += 1
            vstart = pos
            while pos < n:
                if body[pos] == "\\" and pos + 1 < n:
                    pos += 2
                    continue
                if body[pos] == '"':
                    break
                pos += 1
            raw = body[vstart:pos]
            value = raw.replace('\\"', '"').replace("\\\\", "\\").replace("\\n", "\n")
            pos += 1
        elif body[pos] == "(":
            vstart = pos
            depth = 1
            pos += 1
            in_string = False
            while pos < n and depth > 0:
                ch = body[pos]
                if in_string:
                    if ch == "\\" and pos + 1 < n:
                        pos += 2
                        continue
                    if ch == '"':
                        in_string = False
                else:
                    if ch == '"':
                        in_string = True
                    elif ch == "(":
                        depth += 1
                    elif ch == ")":
                        depth -= 1
                pos += 1
            value = body[vstart:pos]
        else:
            vstart = pos
            while pos < n and body[pos] != ";":
                pos += 1
            value = body[vstart:pos].strip()
        while pos < n and body[pos] in " \t\n\r;":
            pos += 1
        out[key] = value
    return out


def _extract_glyph_refs(code, class_members, source_glyphs):
    """Return the set of source-glyph names referenced inside a chunk of FEA code.

    Resolves @ClassName references via `class_members`. Tokens that aren't in
    `source_glyphs` are dropped (filters out keywords, script tags, lookup names).
    """
    if not code:
        return set()
    code = re.sub(r"#.*", "", code)  # strip line comments
    refs = set()
    for m in re.finditer(r"@([A-Za-z_][\w.\-]*)", code):
        refs.update(class_members.get(m.group(1), ()))
    text = re.sub(r"@[A-Za-z_][\w.\-]*", "", code)
    for tok in re.findall(r"[A-Za-z_][\w.\-]*", text):
        if tok in _FEA_KEYWORDS:
            continue
        if tok in source_glyphs:
            refs.add(tok)
    return refs


_SUB_LINE_RE = re.compile(r"^\s*sub\s+(.+?)\s+by\s+(.+?)\s*;\s*$")


def _filter_feature_rules(code, keep_glyphs):
    """Strip per-rule substitution lines whose inputs aren't in the proof subset.

    Walks `code` line-by-line. For each simple `sub X by Y;` (and ligature /
    decomposition / contextual variants):
      - If any input glyph isn't in `keep_glyphs`, the rule can't fire over the
        proof subset — drop the line entirely (it's irrelevant).
      - If all inputs are in keep_glyphs but some output isn't, the rule is
        relevant but broken — drop the line and record the missing outputs.
      - Otherwise, keep the line.

    Rules using `@class` refs or `[bracket]` lists are left untouched (we don't
    try to filter their member lists). Non-`sub` lines (lookup wrappers, script
    directives, comments, blanks) pass through unchanged.

    Returns `(filtered_code, missing_outputs, complex)`. `complex` is True iff
    the code contained any class/bracket rule we couldn't introspect — the
    caller can treat such features more conservatively.
    """
    if not code:
        return code, set(), False
    out_lines = []
    missing = set()
    complex_seen = False
    for raw in code.splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            out_lines.append(raw)
            continue
        if not s.startswith("sub"):
            out_lines.append(raw)
            continue
        # Rules using class references or bracket lists are kept as-is — we'd
        # need a real FEA parser to safely rewrite them.
        if "@" in s or "[" in s:
            complex_seen = True
            out_lines.append(raw)
            continue
        m = _SUB_LINE_RE.match(s)
        if not m:
            out_lines.append(raw)
            continue
        lhs_tokens = m.group(1).split()
        rhs_tokens = m.group(2).split()
        # Strip context apostrophes (e.g. `sub a' b by c.alt;`) for membership tests.
        inputs = [t.rstrip("'") for t in lhs_tokens]
        if not all(g in keep_glyphs for g in inputs):
            # Rule references an input glyph that isn't in the proof — drop silently.
            continue
        outputs_missing = [g for g in rhs_tokens if g not in keep_glyphs]
        if outputs_missing:
            missing.update(outputs_missing)
            continue
        out_lines.append(raw)
    return "\n".join(out_lines), missing, complex_seen


def _read_source_glyph_names(pkg_path):
    """Return every glyph name listed in order.plist (the source's full inventory)."""
    order_path = os.path.join(pkg_path, "order.plist")
    with open(order_path) as f:
        text = f.read()
    names = set()
    for line in text.split("\n"):
        line = line.strip().rstrip(",")
        if line in ("(", ")") or not line:
            continue
        names.add(line.strip('"'))
    return names


def _build_feature_inventory(fontinfo_text, source_glyphs, keep_glyphs):
    """Return (inventory, drop_tags).

    inventory: list of {tag, name, status, disabled, missingGlyphs[], referencedGlyphCount}.
    drop_tags: feature tags that should NOT compile (disabled or with missing glyphs).
    """
    class_entries = [_parse_entry_dict(e) for e in _walk_array_dicts(fontinfo_text, "classes")]
    feature_entries = [_parse_entry_dict(e) for e in _walk_array_dicts(fontinfo_text, "features")]

    class_members = {}
    for c in class_entries:
        cname = c.get("name", "").strip('"')
        members = set(c.get("code", "").split())
        class_members[cname] = members & source_glyphs

    inventory = []
    drop_tags = set()
    seen_tags = []
    filtered_codes = {}  # tag -> last filtered code (only meaningful for compiled features)
    for f in feature_entries:
        tag = f.get("tag", "").strip('"')
        if not tag:
            continue
        code = f.get("code", "")
        disabled = f.get("disabled", "0") == "1"
        # Rule-level filter: drop substitutions whose inputs aren't in the proof
        # subset. `missing` is now only the outputs that an _active_ rule wanted
        # to produce but doesn't have — i.e., glyphs we actually need to draw
        # for the feature to ship over the proof subset.
        filtered_code, missing_set, has_complex_rules = _filter_feature_rules(code, keep_glyphs)
        missing = sorted(missing_set)
        code_no_comments = re.sub(r"#.*", "", code)
        has_class_ref = bool(re.search(r"@[A-Za-z_][\w.\-]*", code_no_comments))
        has_external_lookup = bool(
            re.search(r"^\s*lookup\s+[A-Za-z_][\w]*\s*;", code_no_comments, re.MULTILINE)
        )
        if disabled:
            status = "disabled"
        elif missing:
            status = "missing-glyphs"
        elif has_class_ref or has_external_lookup:
            status = "needs-environment"
        else:
            status = "compiled"
            filtered_codes[tag] = filtered_code
        if status != "compiled":
            drop_tags.add(tag)
        # Multiple feature entries can share a tag (one per script/language pair);
        # collapse into one inventory row per tag, downgrading status pessimistically.
        existing = next((x for x in inventory if x["tag"] == tag), None)
        active_ref_count = sum(
            1 for line in (filtered_code or "").splitlines()
            if line.strip().startswith("sub ")
        )
        if existing is None:
            inventory.append({
                "tag": tag,
                "name": FEATURE_NAMES.get(tag, tag),
                "status": status,
                "disabled": disabled,
                "missingGlyphs": missing[:25],
                "activeRuleCount": active_ref_count,
            })
            seen_tags.append(tag)
        else:
            existing["activeRuleCount"] = max(existing["activeRuleCount"], active_ref_count)
            existing["disabled"] = existing["disabled"] or disabled
            for g in missing:
                if g not in existing["missingGlyphs"] and len(existing["missingGlyphs"]) < 25:
                    existing["missingGlyphs"].append(g)
            order = {"compiled": 0, "needs-environment": 1, "missing-glyphs": 2, "disabled": 3}
            if order[status] > order[existing["status"]]:
                existing["status"] = status
    return inventory, drop_tags, filtered_codes


def _replace_code_field(entry_body, new_code):
    """Replace the `code = "…";` string in an entry body with `new_code`.

    Re-escapes `\\` → `\\\\` and `"` → `\\"` for OpenStep plist quoted-string form.
    Leaves embedded newlines untouched (Glyphs writes raw newlines inside strings).
    """
    m = re.search(r'(code\s*=\s*)"', entry_body)
    if not m:
        return entry_body
    start = m.end()
    pos = start
    while pos < len(entry_body):
        if entry_body[pos] == "\\" and pos + 1 < len(entry_body):
            pos += 2
            continue
        if entry_body[pos] == '"':
            break
        pos += 1
    end = pos
    escaped = new_code.replace("\\", "\\\\").replace('"', '\\"')
    return entry_body[:start] + escaped + entry_body[end:]


def _strip_features_by_tag(text, drop_tags, replace_codes=None):
    """Rewrite the `features = (…)` block, removing entries whose tag is in `drop_tags`.

    For each kept entry whose tag is in `replace_codes` (a {tag: new_code} dict),
    rewrite that entry's `code = "…";` value to the new code. Useful for swapping
    in rule-filtered code so only proof-subset-relevant substitutions ship.

    Returns the new text. If every entry is dropped, the whole `features = (…);`
    block is removed (matches the behaviour of `_strip_plist_key`).
    """
    replace_codes = replace_codes or {}
    m = re.search(r"^features\s*=\s*\(", text, re.MULTILINE)
    if not m:
        return text
    block_start = m.start()
    pos = m.end()
    n = len(text)
    paren_depth = 1
    brace_depth = 0
    entry_start = -1
    in_string = False
    kept_bodies = []
    while pos < n and paren_depth > 0:
        ch = text[pos]
        if in_string:
            if ch == "\\" and pos + 1 < n:
                pos += 2
                continue
            if ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "(":
                paren_depth += 1
            elif ch == ")":
                paren_depth -= 1
            elif ch == "{":
                if brace_depth == 0 and paren_depth == 1:
                    entry_start = pos
                brace_depth += 1
            elif ch == "}":
                brace_depth -= 1
                if brace_depth == 0 and paren_depth == 1 and entry_start >= 0:
                    body = text[entry_start : pos + 1]
                    parsed = _parse_entry_dict(text[entry_start + 1 : pos])
                    tag = parsed.get("tag", "").strip('"')
                    if tag and tag not in drop_tags:
                        if tag in replace_codes:
                            body = _replace_code_field(body, replace_codes[tag])
                        kept_bodies.append(body)
                    entry_start = -1
        pos += 1
    # `pos` is now just after the closing ')'. There may be a trailing `;`.
    after_block_end = pos
    while after_block_end < n and text[after_block_end] in " \t":
        after_block_end += 1
    if after_block_end < n and text[after_block_end] == ";":
        after_block_end += 1
    # Consume the trailing newline so we don't leave a blank line.
    if after_block_end < n and text[after_block_end] == "\n":
        after_block_end += 1

    if not kept_bodies:
        return text[:block_start] + text[after_block_end:]
    rebuilt = "features = (\n" + ",\n".join(kept_bodies) + "\n);\n"
    return text[:block_start] + rebuilt + text[after_block_end:]


def _strip_plist_key(text, key):
    """Remove a top-level key and its value from Glyphs plist text.

    Handles values that are arrays (...) or dicts {...} using paren/brace counting.
    """
    lines = text.split("\n")
    result = []
    skipping = False
    depth = 0

    for line in lines:
        stripped = line.strip()
        if not skipping and stripped.startswith(f"{key} = "):
            skipping = True
            depth = stripped.count("(") + stripped.count("{") - stripped.count(")") - stripped.count("}")
            if depth <= 0:
                skipping = False
            continue
        if skipping:
            depth += stripped.count("(") + stripped.count("{") - stripped.count(")") - stripped.count("}")
            if depth <= 0:
                skipping = False
            continue
        result.append(line)

    return "\n".join(result)


def _strip_backgrounds(text):
    """Remove all 'background = { ... };' blocks from glyph file text."""
    lines = text.split("\n")
    result = []
    skip_depth = 0
    skipping = False

    for line in lines:
        stripped = line.strip()
        if not skipping and stripped.startswith("background = {"):
            skipping = True
            skip_depth = 1
            continue
        if skipping:
            skip_depth += stripped.count("{") - stripped.count("}")
            if skip_depth <= 0:
                skipping = False
            continue
        result.append(line)

    return "\n".join(result)


def _parse_glyph(filepath):
    """Parse a .glyph file, returning (glyphname, color, unicode_value).

    color and unicode_value may be None.
    """
    with open(filepath, "r", encoding="utf-8") as f:
        text = f.read()

    m = re.search(r'glyphname = (.+);', text)
    glyphname = m.group(1).strip().strip('"') if m else None

    m = re.search(r'^color = (.+);', text, re.MULTILINE)
    color = m.group(1).strip() if m else None

    m = re.search(r'unicode = (\d+);', text)
    unicode_val = int(m.group(1)) if m else None

    return glyphname, color, unicode_val


def build_font(source_path, *, name=None, proof_family=None, references=None, defaults=None):
    """Build the proof font + all preview manifests. Returns True on success.

    Args:
      source_path: absolute path to the typeface .glyphspackage / .glyphs source.
      name: project name (defaults to source filename stem). Used as the default
            headline string and to derive the proof font family.
      proof_family: font-family name for the proof font (defaults to "<name> Proof").
      references: list of dicts, each like
            {"family": "Verdana", "files": {"regular": "/abs/path.ttf", ...}}
            Style keys: "regular" | "bold" | "italic" | "boldItalic".
      defaults: optional {"headline": str, "body": str} overrides for the
                React app's initial editable contents.
    """
    if not os.path.isdir(source_path):
        print(f"Error: source not found: {source_path}", file=sys.stderr)
        return False

    pkg_name = os.path.basename(os.path.normpath(source_path))
    project_name = name or os.path.splitext(pkg_name)[0]
    proof_family = proof_family or f"{project_name} Proof"
    output_path = str(OUTPUT_PATH)
    references = references or []

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    tmp_dir = tempfile.mkdtemp(prefix=".proof-build-")
    try:
        # 1. Copy source package to temp
        tmp_pkg = os.path.join(tmp_dir, pkg_name)
        shutil.copytree(source_path, tmp_pkg)

        glyphs_dir = os.path.join(tmp_pkg, "glyphs")

        # 2. Strip background layers (if any exist in source)
        for fname in os.listdir(glyphs_dir):
            if not fname.endswith(".glyph"):
                continue
            fpath = os.path.join(glyphs_dir, fname)
            with open(fpath, "r", encoding="utf-8") as f:
                text = f.read()
            if "background = {" in text:
                text = _strip_backgrounds(text)
                with open(fpath, "w", encoding="utf-8") as f:
                    f.write(text)

        # 3. Scan glyphs and decide which to keep
        keep = set(ESSENTIAL_GLYPHS)
        all_glyphs = set()
        available_codepoints = set()

        for fname in os.listdir(glyphs_dir):
            if not fname.endswith(".glyph"):
                continue
            fpath = os.path.join(glyphs_dir, fname)
            glyphname, color, unicode_val = _parse_glyph(fpath)
            if glyphname is None:
                continue
            all_glyphs.add((glyphname, fname))
            if color in PROOF_COLORS or glyphname in ESSENTIAL_GLYPHS:
                keep.add(glyphname)
                if unicode_val is not None:
                    available_codepoints.add(unicode_val)

        # 4. Delete non-kept glyph files
        removed = 0
        for glyphname, fname in all_glyphs:
            if glyphname not in keep:
                os.remove(os.path.join(glyphs_dir, fname))
                removed += 1

        print(f"Kept {len(keep)} glyphs, removed {removed}")

        # 5. Build feature inventory and selectively strip unviable features
        # from fontinfo.plist. A feature is "compiled" when it isn't disabled in
        # source AND every glyph it references is in `keep`. Anything else is
        # dropped from the temp fontinfo (so fontc doesn't choke on missing-glyph
        # references) but recorded in the inventory for the proof app's dropdown.
        fontinfo_path = os.path.join(tmp_pkg, "fontinfo.plist")
        with open(fontinfo_path, "r", encoding="utf-8") as f:
            fontinfo_text = f.read()

        source_all_glyphs = _read_source_glyph_names(str(source_path))
        feature_inventory, drop_tags, filtered_codes = _build_feature_inventory(
            fontinfo_text, source_all_glyphs, keep
        )
        kept_feature_tags = {f["tag"] for f in feature_inventory if f["status"] == "compiled"}

        # Always strip classes / featurePrefixes — they reference removed glyphs
        # and break fontc. The compile-classifier above only marks a feature as
        # `compiled` when it has no @class / external-lookup dependencies, so
        # remaining features stand alone.
        for key in ("classes", "featurePrefixes"):
            fontinfo_text = _strip_plist_key(fontinfo_text, key)
        if kept_feature_tags:
            # Swap each kept feature's code for the rule-filtered version so it
            # only contains substitutions whose inputs are in the proof subset.
            replace_codes = {t: filtered_codes[t] for t in kept_feature_tags if t in filtered_codes}
            fontinfo_text = _strip_features_by_tag(fontinfo_text, drop_tags, replace_codes)
        else:
            fontinfo_text = _strip_plist_key(fontinfo_text, "features")

        with open(fontinfo_path, "w", encoding="utf-8") as f:
            f.write(fontinfo_text)

        print(
            f"Features: {len(kept_feature_tags)} compiled, "
            f"{len([f for f in feature_inventory if f['status'] == 'missing-glyphs'])} missing-glyphs, "
            f"{len([f for f in feature_inventory if f['status'] == 'disabled'])} disabled-in-source"
        )
        if kept_feature_tags:
            print(f"  Compiling: {sorted(kept_feature_tags)}")

        # 6. Rewrite order.plist to match kept glyphs
        order_path = os.path.join(tmp_pkg, "order.plist")
        with open(order_path, "r", encoding="utf-8") as f:
            order_text = f.read()

        order_lines = order_text.strip().split("\n")
        filtered_names = []
        for line in order_lines:
            line = line.strip().rstrip(",")
            if line in ("(", ")") or not line:
                continue
            if line in keep:
                filtered_names.append(line)

        new_order = "(\n" + ",\n".join(filtered_names) + "\n)\n"
        with open(order_path, "w", encoding="utf-8") as f:
            f.write(new_order)

        # 7. Compile with fontc
        print("Compiling with fontc...")
        result = subprocess.run(
            ["fontc", tmp_pkg, "-o", output_path],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"fontc failed (exit {result.returncode}):", file=sys.stderr)
            if result.stderr:
                print(result.stderr, file=sys.stderr)
            if result.stdout:
                print(result.stdout, file=sys.stderr)
            return False

        print(f"Built: {OUTPUT_PATH.relative_to(PREVIEW_DIR.parent) if str(OUTPUT_PATH).startswith(str(PREVIEW_DIR.parent)) else OUTPUT_PATH}")

        # 8. Write available characters manifest
        manifest_path = os.path.join(os.path.dirname(output_path), "available-chars.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(sorted(available_codepoints), f)
        print(f"Manifest: {len(available_codepoints)} codepoints written")

        # 8b. Write features manifest. Tag-level "compiled" status reflects what
        # the build script intended to keep; we cross-check against the actual
        # GSUB feature list in the compiled font so the dropdown only marks a
        # feature green when it really shipped.
        try:
            from fontTools.ttLib import TTFont
            shipped_tags = set()
            f = TTFont(output_path)
            for table in ("GSUB", "GPOS"):
                if table in f and f[table].table.FeatureList:
                    shipped_tags.update(
                        fr.FeatureTag for fr in f[table].table.FeatureList.FeatureRecord
                    )
            f.close()
        except Exception:
            shipped_tags = set()

        for feat in feature_inventory:
            if feat["status"] == "compiled" and feat["tag"] not in shipped_tags:
                # Build script kept it but fontc didn't ship it (e.g. fontc
                # dropped an empty / no-op feature). Downgrade so the dropdown
                # doesn't lie to the user.
                feat["status"] = "missing-glyphs" if feat["missingGlyphs"] else "disabled"

        features_path = os.path.join(os.path.dirname(output_path), "available-features.json")
        with open(features_path, "w", encoding="utf-8") as f:
            json.dump(feature_inventory, f, indent=2, sort_keys=False)
        print(f"Features manifest: {len(feature_inventory)} features written")

        # 9. Copy reference fonts into public/ref/ and write proof-config.json
        ref_entries = _copy_references(references)
        _write_proof_config(
            project_name=project_name,
            proof_family=proof_family,
            ref_entries=ref_entries,
            defaults=defaults or {},
        )
        print(f"Config: {CONFIG_OUTPUT.relative_to(PREVIEW_DIR.parent)}")

        # 10. Print axis info
        try:
            from fontTools.ttLib import TTFont
            font = TTFont(output_path)
            if "fvar" in font:
                for axis in font["fvar"].axes:
                    print(f"  {axis.axisTag}: {axis.minValue}-{axis.maxValue}, default={axis.defaultValue}")
            font.close()
        except ImportError:
            pass

        return True

    finally:
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir)


def _copy_references(references):
    """Copy each reference font into preview/public/ref/ and return the
    list of config entries to write into proof-config.json. Skips entries
    whose source file doesn't exist (with a warning).
    """
    if not references:
        return []
    os.makedirs(REF_OUTPUT_DIR, exist_ok=True)
    out = []
    for ref in references:
        family = ref.get("family")
        files  = ref.get("files") or {}
        config_files = {}
        for style_key, src in files.items():
            if not os.path.isfile(src):
                print(f"  Warning: reference font missing, skipped: {src}", file=sys.stderr)
                continue
            # Normalize filename: <Family>-<Style>.ttf. Keeps the URL
            # predictable and avoids leaking source-path layout.
            ext = os.path.splitext(src)[1] or ".ttf"
            dest_name = f"{family.replace(' ', '_')}-{style_key}{ext}"
            dest_path = REF_OUTPUT_DIR / dest_name
            shutil.copyfile(src, dest_path)
            config_files[style_key] = f"ref/{dest_name}"
        if config_files:
            out.append({"family": family, "files": config_files})
    return out


def _write_proof_config(*, project_name, proof_family, ref_entries, defaults):
    """Write proof-config.json — the runtime bridge the React app fetches."""
    cfg = {
        "project":      {"name": project_name},
        "proofFont":    {
            "family": proof_family,
            "label":  f"{proof_family} (proof subset)",
            "file":   OUTPUT_PATH.name,
            "weight": "100 900",
            "style":  "normal",
        },
        "defaults": {
            "headline": defaults.get("headline", project_name),
            "body":     defaults.get("body",
                "The quick brown fox jumps over the lazy dog "
                "Pack my box with five dozen liquor jugs "
                "How vexingly quick daft zebras jump"),
        },
        "referenceFonts": ref_entries,
    }
    with open(CONFIG_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def watch_and_rebuild(source_path, watch_path, **build_kwargs):
    """Watch for .glyph file changes and rebuild the font."""
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler

    class GlyphChangeHandler(FileSystemEventHandler):
        def __init__(self):
            self._last_build = 0
            self._debounce = 0.5

        def on_modified(self, event):
            if event.is_directory:
                return
            if not event.src_path.endswith(".glyph"):
                return
            self._trigger_rebuild()

        def on_created(self, event):
            if not event.is_directory and event.src_path.endswith(".glyph"):
                self._trigger_rebuild()

        def _trigger_rebuild(self):
            now = time.time()
            if now - self._last_build < self._debounce:
                return
            self._last_build = now
            print("\n--- Change detected, rebuilding... ---")
            build_font(source_path, **build_kwargs)

    handler = GlyphChangeHandler()
    observer = Observer()
    observer.schedule(handler, watch_path, recursive=True)
    observer.start()
    print(f"Watching {watch_path} for changes (Ctrl+C to stop)...")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


def _parse_reference_arg(value):
    """`Verdana:regular=/path/to/Verdana.ttf` → ('Verdana', 'regular', '/path/...').
    Bare `Verdana=/path` defaults the style to `regular`.
    """
    if "=" not in value:
        raise argparse.ArgumentTypeError(
            f"--reference expects FAMILY[:STYLE]=PATH, got: {value!r}"
        )
    spec, path = value.split("=", 1)
    if ":" in spec:
        family, style = spec.split(":", 1)
    else:
        family, style = spec, "regular"
    style = style.strip()
    valid_styles = {"regular", "bold", "italic", "boldItalic"}
    # Allow snake_case for boldItalic since `bold_italic` reads more naturally.
    if style == "bold_italic":
        style = "boldItalic"
    if style not in valid_styles:
        raise argparse.ArgumentTypeError(
            f"--reference style must be one of {sorted(valid_styles)}, got: {style!r}"
        )
    return (family.strip(), style, path.strip())


def _references_from_audit_config():
    """Fallback: build a single Reference-family entry from any [instances.*]
    `ref = "..."` paths in ~/.glyph-audit/config.toml. Each instance is
    treated as one style (regular/bold by name match). Returns [] if the
    config doesn't exist or has no usable entries.
    """
    if not USER_CONFIG.exists():
        return []
    try:
        import tomllib
    except ImportError:
        return []
    try:
        with open(USER_CONFIG, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return []
    instances = data.get("instances") or {}
    style_map = {}
    for inst_name, entry in instances.items():
        if not isinstance(entry, dict):
            continue
        ref_path = entry.get("ref")
        if not isinstance(ref_path, str) or not os.path.isfile(ref_path):
            continue
        n = inst_name.lower()
        if "bold" in n and "italic" in n: style_map["boldItalic"] = ref_path
        elif "bold" in n:                 style_map["bold"]       = ref_path
        elif "italic" in n:               style_map["italic"]     = ref_path
        else:                             style_map["regular"]    = ref_path
    if not style_map:
        return []
    # Read the family name out of the regular file's name table if we can.
    family = "Reference"
    try:
        from fontTools.ttLib import TTFont
        any_path = style_map.get("regular") or next(iter(style_map.values()))
        f = TTFont(any_path)
        for record in f["name"].names:
            if record.nameID == 1:  # family
                family = str(record)
                break
        f.close()
    except Exception:
        pass
    return [{"family": family, "files": style_map}]


def main():
    parser = argparse.ArgumentParser(
        description="Build the proof font + manifests + runtime config "
                    "for the GlyphAudit Preview Vite app.",
    )
    parser.add_argument(
        "--source", required=True,
        help="Path to the typeface .glyphspackage / .glyphs source.",
    )
    parser.add_argument(
        "--name",
        help="Project name (default: source filename stem). Used as the "
             "default headline and to derive the proof font family name.",
    )
    parser.add_argument(
        "--proof-family",
        help="Override the proof font family name (default: '<NAME> Proof').",
    )
    parser.add_argument(
        "--reference", action="append", default=[], type=_parse_reference_arg,
        metavar="FAMILY[:STYLE]=PATH",
        help="Reference font to bundle for the comparison panel. Style is "
             "one of regular|bold|italic|boldItalic (default: regular). "
             "Repeat for each style. If none given, falls back to any "
             "[instances.*] entries in ~/.glyph-audit/config.toml.",
    )
    parser.add_argument(
        "--headline", help="Override the default headline text.",
    )
    parser.add_argument(
        "--body", help="Override the default body text.",
    )
    parser.add_argument(
        "--watch", action="store_true",
        help="Watch the source for changes and rebuild (requires watchdog).",
    )
    args = parser.parse_args()

    if not shutil.which("fontc"):
        print("Error: fontc not found. Install with: pip install fontc", file=sys.stderr)
        sys.exit(1)

    source_path = os.path.abspath(args.source)

    # Group --reference flags by family.
    refs_by_family = {}
    for family, style, path in args.reference:
        refs_by_family.setdefault(family, {"family": family, "files": {}})
        refs_by_family[family]["files"][style] = os.path.abspath(path)
    references = list(refs_by_family.values()) or _references_from_audit_config()

    defaults = {}
    if args.headline is not None: defaults["headline"] = args.headline
    if args.body is not None:     defaults["body"]     = args.body

    build_kwargs = dict(
        name=args.name,
        proof_family=args.proof_family,
        references=references,
        defaults=defaults,
    )
    success = build_font(source_path, **build_kwargs)
    if not success and not args.watch:
        sys.exit(1)

    if args.watch:
        watch_and_rebuild(
            source_path,
            os.path.join(source_path, "glyphs"),
            **build_kwargs,
        )


if __name__ == "__main__":
    main()
