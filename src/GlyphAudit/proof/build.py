"""Subset a Glyphs source down to a proof TTF using fontc.

The heavy-lifting library behind `glyph-audit proof build/serve`. Callers
pass explicit paths + a color set; nothing here reads config files, spawns
subprocesses, or knows about a specific font project. See
`GlyphAudit.proof.config` for the TOML config schema.

Filters .glyph files by color, transitively includes referenced components
so fontc doesn't panic on missing dependencies, strips broken features
(rules whose glyphs got filtered out), compiles with fontc, then applies
two post-fix passes (see `_apply_postfixes`).
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

# Re-export the color palette + default so callers that just want the
# labels don't have to know about the config submodule.
from .config import (
    GLYPHS_COLORS,
    DEFAULT_PROOF_COLORS,
    DEFAULT_ESSENTIAL_GLYPHS as ESSENTIAL_GLYPHS,
    validate_colors,
    normalize_color,
)


def output_paths_for(source_pkg, basename):
    """Return (ttf_name, chars_manifest_name, features_manifest_name).

    Italic sources (basename contains 'Italic', case-insensitive) get an
    `-italic` suffix so roman and italic outputs coexist in the same
    directory. All three filenames use the same suffix scheme.
    """
    is_italic = "italic" in os.path.basename(source_pkg).lower()
    suffix = "-italic" if is_italic else ""
    return (
        f"{basename}{suffix}.ttf",
        f"available-chars{suffix}.json",
        f"available-features{suffix}.json",
    )


def write_proof_config(
    output_dir,
    family_name,
    output_basename,
    sources,
    references=(),
    copy_references=True,
):
    """Emit `proof-config.json` describing what's on disk for the web app.

    Also copies each reference slot's TTF into `output_dir` when
    `copy_references=True` so the web app can fetch them from same-origin
    paths (browsers won't load fonts cross-origin without CORS headers).

    `sources`   — iterable of source .glyphspackage basenames. Used only to
                  decide which face slots (roman / italic) get written into
                  the manifest.
    `references` — iterable of GlyphAudit.proof.config.Reference objects.
                  Empty when the user hasn't configured any.

    Returns the manifest dict (also written to `<output_dir>/proof-config.json`).
    """
    output_dir = os.fspath(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    faces = {}
    for src in sources:
        pkg = os.path.basename(os.fspath(src).rstrip(os.sep))
        ttf, chars, features = output_paths_for(pkg, output_basename)
        slot = "italic" if "italic" in pkg.lower() else "roman"
        # If both a roman and italic source point at the same slot (unlikely
        # but possible with typos), the last one wins — non-fatal.
        faces[slot] = {"ttf": f"/{ttf}", "chars": f"/{chars}", "features": f"/{features}"}

    ref_entries = []
    for ref in references:
        slot_entries = []
        for s in ref.slots:
            src_path = s.path
            if copy_references and os.path.isfile(src_path):
                dest_name = os.path.basename(src_path)
                dest_path = os.path.join(output_dir, dest_name)
                try:
                    # Copy only when the source is newer or the dest is
                    # missing — avoids rewriting the file on every build.
                    if (not os.path.exists(dest_path)
                            or os.path.getmtime(src_path) > os.path.getmtime(dest_path)):
                        shutil.copy2(src_path, dest_path)
                except OSError as e:
                    print(f"  Reference copy failed for {src_path}: {e}", file=sys.stderr)
            else:
                dest_name = os.path.basename(src_path)
            entry = {"file": f"/{dest_name}", "weight": s.weight, "style": s.style}
            if s.slot == "variable":
                # A variable file legitimately covers 400/700, normal/italic.
                # Emit the file once with `variable` style; the web app expands
                # it into four @font-face entries (weight 100-900 × normal +
                # italic) so the browser can interpolate freely.
                entry["style"] = "variable"
            slot_entries.append(entry)
        ref_entries.append({"name": ref.name, "slots": slot_entries})

    manifest = {
        "familyName": family_name,
        "faces": faces,
        "references": ref_entries,
    }
    path = os.path.join(output_dir, "proof-config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    return manifest


def _slugify(name: str) -> str:
    """Reference-family name → filesystem-safe slug."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def write_width_manifests(output_dir, output_basename, sources, references,
                          tolerance_units: int = 1):
    """Emit per-(face, reference) advance-width diff manifests.

    For every proof face (roman + italic) and every reference family that
    has a matching-style slot, walk the codepoint intersection of the two
    fonts and record entries where `|proof - reference| > tolerance_units`.

    File names: `widths-<face>-<slug>.json`. Web app fetches on reference-
    dropdown change and paints matched glyphs with the amber
    `.width-mismatch` treatment.

    Silent no-op when fontTools isn't importable (unusual — it's a hard
    dependency of the tool — but keeps the build from crashing if
    reference paths fail to load).
    """
    try:
        from fontTools.ttLib import TTFont
    except ImportError:  # pragma: no cover
        return {}

    output_dir = os.fspath(output_dir)

    def _face_widths(ttf_path):
        try:
            f = TTFont(ttf_path)
            hmtx = f["hmtx"]
            cmap = f.getBestCmap()
            widths = {cp: hmtx[name][0] for cp, name in cmap.items() if name in hmtx.metrics}
            f.close()
            return widths
        except Exception as e:
            print(f"  width-manifest: couldn't read {ttf_path}: {e}", file=sys.stderr)
            return {}

    # Build a {face_slot: proof_widths} map. Face slot names match the
    # keys in `proof-config.json`'s `faces` block ('roman' / 'italic').
    proof_widths_by_face = {}
    for src in sources:
        pkg = os.path.basename(os.fspath(src).rstrip(os.sep))
        ttf_name, _, _ = output_paths_for(pkg, output_basename)
        ttf_path = os.path.join(output_dir, ttf_name)
        if not os.path.isfile(ttf_path):
            continue
        slot = "italic" if "italic" in pkg.lower() else "roman"
        proof_widths_by_face[slot] = _face_widths(ttf_path)

    # Pick the reference slot to pair against each proof face. Prefer an
    # exact style match, fall back to `regular` for both when nothing else
    # is available (matches Verdana's behaviour when only one style ships).
    def _pick_slot(ref, face_slot):
        style_target = "italic" if face_slot == "italic" else "normal"
        for s in ref.slots:
            if s.style == style_target and s.slot in ("regular", "italic"):
                return s
        for s in ref.slots:
            if s.slot == "regular":
                return s
        return ref.slots[0] if ref.slots else None

    written = {}
    for ref in references:
        slug = _slugify(ref.name)
        for face_slot, proof_widths in proof_widths_by_face.items():
            picked = _pick_slot(ref, face_slot)
            if picked is None or not os.path.isfile(picked.path):
                continue
            ref_widths = _face_widths(picked.path)
            entries = []
            for cp, pw in proof_widths.items():
                rw = ref_widths.get(cp)
                if rw is None:
                    continue
                delta = pw - rw
                if abs(delta) > tolerance_units:
                    entries.append({
                        "cp": cp,
                        "proof": pw,
                        "ref": rw,
                        "delta": delta,
                    })
            entries.sort(key=lambda e: -abs(e["delta"]))
            fname = f"widths-{face_slot}-{slug}.json"
            path = os.path.join(output_dir, fname)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(entries, f)
            written[(face_slot, ref.name)] = fname
    return written

# Human-readable names for the OT feature tags this source uses.
# (Only those that actually appear in Velarium's source are listed; unknown tags
# fall back to the tag itself.)
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


def _flatten_fea_tokens(s):
    """Split an FEA side (LHS or RHS of `sub … by …`) into glyph-name tokens.

    Bracket lists `[a b c]` yield each contained token; context apostrophes
    `x'` are stripped. Class refs (`@foo`) are returned literally so the caller
    can decide whether to bail. Returns [] if the string can't be parsed.
    """
    tokens = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c.isspace():
            i += 1
            continue
        if c == "[":
            end = s.find("]", i)
            if end < 0:
                return []
            for t in s[i + 1:end].split():
                tokens.append(t.rstrip("'"))
            i = end + 1
        else:
            j = i
            while j < n and not s[j].isspace() and s[j] != "[":
                j += 1
            tokens.append(s[i:j].rstrip("'"))
            i = j
    return tokens


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
        # `@class` refs need a real FEA parser to safely rewrite (member lists
        # can change under filtering) — leave those rules untouched and flag the
        # feature as complex. Bracket lists `[a b c]`, however, are just
        # syntactic sugar for enumerated alternates: we can walk them.
        if "@" in s:
            complex_seen = True
            out_lines.append(raw)
            continue
        m = _SUB_LINE_RE.match(s)
        if not m:
            out_lines.append(raw)
            continue
        inputs = _flatten_fea_tokens(m.group(1))
        outputs = _flatten_fea_tokens(m.group(2))
        if not inputs or not outputs:
            # Parse failure — keep the rule untouched, flag complex.
            complex_seen = True
            out_lines.append(raw)
            continue
        if not all(g in keep_glyphs for g in inputs):
            # Rule references an input glyph that isn't in the proof — drop silently.
            continue
        outputs_missing = [g for g in outputs if g not in keep_glyphs]
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
        # Count `sub` rules that survived filtering. Early because we also
        # use this to decide "did filtering leave anything worth compiling?"
        active_sub_count = sum(
            1 for line in (filtered_code or "").splitlines()
            if line.strip().startswith("sub ")
        )
        # If every rule got filtered out, what's left is a scaffold of empty
        # `lookup FOO { } FOO;` blocks — a fea-syntax error that would kill
        # the whole compile. Demote to "missing-glyphs" so the feature is
        # dropped rather than passed through empty.
        code_had_subs = any(
            line.strip().startswith("sub ") for line in code.splitlines()
        )
        if disabled:
            status = "disabled"
        elif missing:
            status = "missing-glyphs"
        elif code_had_subs and active_sub_count == 0:
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
        active_ref_count = active_sub_count
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


def build_font(
    source_path,
    output_dir,
    output_basename,
    proof_colors=None,
    essential_glyphs=None,
):
    """Build a subset variable font from a Glyphs source. Returns True on success.

    Args:
        source_path:     absolute path to the `.glyphspackage` (or `.glyphs`).
        output_dir:      absolute path to the directory where the TTF and JSON
                         manifests should be written. Created if missing.
        output_basename: basename for outputs (e.g. `Merriweather-proof`);
                         italic sources get an `-italic` suffix appended.
        proof_colors:    iterable of GLYPHS_COLORS keys to include; defaults
                         to yellow + light green.
        essential_glyphs: names that survive the color filter unconditionally
                         (e.g. `_notdef`, `space`).

    On success the following files land under `output_dir`:
        <basename>[-italic].ttf
        available-chars[-italic].json
        available-features[-italic].json
    """
    if proof_colors is None:
        proof_colors = DEFAULT_PROOF_COLORS
    proof_colors = frozenset(proof_colors)
    if essential_glyphs is None:
        essential_glyphs = ESSENTIAL_GLYPHS
    essential_glyphs = frozenset(essential_glyphs)

    source_path = os.fspath(source_path)
    output_dir = os.fspath(output_dir)
    pkg_name = os.path.basename(source_path.rstrip(os.sep))
    ttf_name, chars_name, features_name = output_paths_for(pkg_name, output_basename)
    output_path = os.path.join(output_dir, ttf_name)

    if not os.path.isdir(source_path):
        print(f"Error: source not found: {source_path}", file=sys.stderr)
        return False

    # Display path — relative to CWD when possible, so build logs stay short.
    try:
        output_rel = os.path.relpath(output_path)
    except ValueError:
        output_rel = output_path

    os.makedirs(output_dir, exist_ok=True)

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
        keep = set(essential_glyphs)
        all_glyphs = set()
        available_codepoints = set()
        components_by_glyph = {}  # glyphname -> set of ref'd component names

        for fname in os.listdir(glyphs_dir):
            if not fname.endswith(".glyph"):
                continue
            fpath = os.path.join(glyphs_dir, fname)
            glyphname, color, unicode_val = _parse_glyph(fpath)
            if glyphname is None:
                continue
            all_glyphs.add((glyphname, fname))
            # Also index component references so we can transitively pull them in.
            with open(fpath, "r", encoding="utf-8") as f:
                text = f.read()
            components_by_glyph[glyphname] = set(re.findall(r"^\s*ref\s*=\s*([^;\s]+)\s*;", text, re.MULTILINE))
            # Uncolored glyphs match the sentinel "none". Otherwise the
            # numeric color value (as a string) must be in the selected set.
            color_key = color if color is not None else "none"
            if color_key in proof_colors or glyphname in essential_glyphs:
                keep.add(glyphname)
                if unicode_val is not None:
                    available_codepoints.add(unicode_val)

        # 3b. Transitive component closure: a yellow-flagged glyph often
        # references un-flagged components (accent bases, decorative marks like
        # currencybar). fontc panics if any referenced component isn't in the
        # subset, so we walk each kept glyph's component list to fixpoint and
        # pull in every reachable dependency. Not included in
        # `available_codepoints` — those glyphs aren't independently proofable,
        # they're just structural.
        source_glyph_names = {g for g, _ in all_glyphs}
        closure_added = set()
        frontier = set(keep)
        while frontier:
            next_frontier = set()
            for g in frontier:
                for ref in components_by_glyph.get(g, ()):
                    ref_clean = ref.strip().strip('"')
                    if ref_clean and ref_clean not in keep and ref_clean in source_glyph_names:
                        keep.add(ref_clean)
                        closure_added.add(ref_clean)
                        next_frontier.add(ref_clean)
            frontier = next_frontier
        if closure_added:
            print(f"Component closure: pulled in {len(closure_added)} untagged dependencies")

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

        source_all_glyphs = _read_source_glyph_names(source_path)
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

        print(f"Built: {output_rel}")

        # 7b. Post-compile normalization: fix two source-side authoring quirks
        # that we know about but haven't yet corrected in the tracked
        # .glyphspackage. When the source is fixed these become no-ops.
        #
        #   (a) USE_TYPO_METRICS (fsSelection bit 7). Without it the browser
        #       picks hhea metrics for line height. Roman's source sets it via
        #       the "Use Typo Metrics" custom parameter; the italic source
        #       doesn't. If we don't force it on, the italic panel sits ~14%
        #       taller than the Roman panel at the same font-size.
        #   (b) Bold-Italic wght axis position. The italic source's Bold-Italic
        #       instance has no explicit weightClass, so fontc emits its
        #       coordinate at wght=900 (matching the "Axis Location" custom
        #       parameter) instead of the standard 700. Any browser request for
        #       font-weight:700 then interpolates 60% of the way to the master
        #       instead of landing on it — Bold-Italic renders visibly lighter
        #       than Bold-Roman. We just relabel the axis: the actual variation
        #       data is keyed on normalized coordinates, so shifting the
        #       maxValue + Bold-Italic instance from 900 → 700 lets font-weight
        #       700 hit the real master without touching a single glyph outline.
        try:
            from fontTools.ttLib import TTFont
            fnt = TTFont(output_path)
            changed = False
            USE_TYPO_METRICS = 0x80
            if not (fnt["OS/2"].fsSelection & USE_TYPO_METRICS):
                fnt["OS/2"].fsSelection |= USE_TYPO_METRICS
                changed = True
                print("  Post-fix: set USE_TYPO_METRICS on OS/2")
            wght_axis = next((a for a in fnt["fvar"].axes if a.axisTag == "wght"), None)
            if wght_axis and wght_axis.maxValue > 700:
                old_max = wght_axis.maxValue
                wght_axis.maxValue = 700
                for inst in fnt["fvar"].instances:
                    if inst.coordinates.get("wght") == old_max:
                        inst.coordinates["wght"] = 700
                # OS/2.usWeightClass tracks the default (regular) weight — leave
                # it alone; only the bold end of the axis moved.
                changed = True
                print(f"  Post-fix: rescaled wght axis max {old_max}→700 (Bold-Italic master now at 700)")
            if changed:
                fnt.save(output_path)
            fnt.close()
        except Exception as e:
            print(f"  Post-fix skipped ({type(e).__name__}: {e})")

        # 8. Write available characters manifest
        manifest_path = os.path.join(os.path.dirname(output_path), chars_name)
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

        features_path = os.path.join(os.path.dirname(output_path), features_name)
        with open(features_path, "w", encoding="utf-8") as f:
            json.dump(feature_inventory, f, indent=2, sort_keys=False)
        print(f"Features manifest: {len(feature_inventory)} features written")

        # 9. Print axis info
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


# Filenames whose changes should NOT trigger a rebuild (noisy, cosmetic, or
# autogenerated). Everything else under the .glyphspackage IS build-affecting:
# .glyph outlines, fontinfo.plist (features/classes/OS-2), kerning.plist, etc.
_WATCH_IGNORE = {"UIState.plist", ".DS_Store", "order.plist"}


def _is_watch_relevant(src_path):
    base = os.path.basename(src_path)
    if base in _WATCH_IGNORE:
        return False
    if base.startswith("."):
        return False
    # Glyphs.app writes to a temp file then renames — accept both the final
    # name and intermediate .plist / .glyph writes.
    if base.endswith(".glyph") or base.endswith(".plist"):
        return True
    return False


def watch_and_rebuild(
    source_paths,
    output_dir,
    output_basename,
    proof_colors=None,
    essential_glyphs=None,
):
    """Watch every path in `source_paths` and rebuild the affected font
    when a build-relevant file changes.

    Args:
        source_paths:     iterable of absolute paths to `.glyphspackage` (or
                          `.glyphs`) directories. Each gets its own debounced
                          handler so editing the roman doesn't retrigger the
                          italic build.
        output_dir, output_basename, proof_colors, essential_glyphs: same as
        `build_font`.

    Blocks until KeyboardInterrupt. Callers running this from a subprocess
    can rely on SIGTERM propagation to unblock it.
    """
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler

    source_paths = [os.fspath(p) for p in source_paths]
    output_dir = os.fspath(output_dir)

    class PackageChangeHandler(FileSystemEventHandler):
        def __init__(self, src_path):
            self.src_path = src_path
            self.pkg_name = os.path.basename(src_path.rstrip(os.sep))
            self._last_build = 0
            self._debounce = 0.5

        def on_any_event(self, event):
            if event.is_directory:
                return
            if event.event_type not in ("created", "modified", "moved"):
                return
            path = getattr(event, "dest_path", None) or event.src_path
            if not _is_watch_relevant(path):
                return
            now = time.time()
            if now - self._last_build < self._debounce:
                return
            self._last_build = now
            print(
                f"\n--- {self.pkg_name}: change detected "
                f"({os.path.basename(path)}), rebuilding... ---"
            )
            build_font(
                self.src_path, output_dir, output_basename,
                proof_colors=proof_colors,
                essential_glyphs=essential_glyphs,
            )

    observer = Observer()
    watched = []
    for src in source_paths:
        if not os.path.isdir(src):
            print(f"Warning: skipping missing source {src}", file=sys.stderr)
            continue
        observer.schedule(PackageChangeHandler(src), src, recursive=True)
        watched.append(src)
    if not watched:
        print("Nothing to watch.", file=sys.stderr)
        return
    observer.start()
    print("Watching (Ctrl+C to stop):")
    for p in watched:
        print(f"  {p}")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
