"""Command-line interface for Glyph Audit (`glyph-audit`).

Usage:

  glyph-audit \\
    --target sources/MyTypeface.glyphspackage \\
    --pair Regular=sources/reference/Reference-Regular.ttf \\
    --pair Bold=sources/reference/Reference-Bold.ttf \\
    --output coverage-report.md

(equivalent to `python -m GlyphAudit ...`)

A "pair" maps a target master name to a reference font.  The reference
half can be:

  * path to a .ttf or .otf
  * path to a .glyphspackage or .glyphs
  * "<Name>-system"   (system-installed font; '-' or ' ' separates
                       family from style: 'Helvetica Bold-system' or
                       'Helvetica-Bold-system')

Append '@axis=value[,axis=value]' to pin a variable font:

    --pair Regular="path/to/Inter[wght].ttf@wght=400"
    --pair Bold="Inter-system@wght=700"

Or define a reusable instance map in ~/.glyph-audit/config.toml and
pass --from-config to skip --pair entirely:

    [instances.Regular]
    ref = "Reference-Regular.ttf"

    [instances.Bold]
    ref = "Inter[wght].ttf"
    axis = { wght = 700 }

Single-master TTF inputs as the target font are also supported — pass
--target PATH and a single --pair "Default=<reference>".
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Optional

from .loaders import load_font
from .comparator import TieredComparator
from .defaults import load_defaults
from .instances import load_instances, InstanceSpec
from .ai.config import ConfigError
from .model import COLOR_FILTERS, GLYPHS_COLOR_NAMES
from .report import write_markdown


# Hard-coded fallbacks. Used only when neither CLI nor config supplies a value.
HARDCODED = {
    "output_no_filter": "glyph-audit-report.md",
    "output_filtered":  "glyph-audit-filtered.md",
    "tolerance":        1.0,
    "title":            "Glyph Audit Report",
    "filter":           "all",
}


def _split_pair(arg: str) -> tuple[str, str]:
    if "=" not in arg:
        raise argparse.ArgumentTypeError(
            f"--pair expected NAME=REFERENCE form, got: {arg!r}"
        )
    name, ref = arg.split("=", 1)
    return name.strip(), ref.strip()


def _build_audit_parser(audit: argparse.ArgumentParser) -> None:
    """Attach the (existing) `--target` / `--pair` / … argument set to
    whatever parser it's given. Split out from `main` so the same argument
    schema powers both the top-level backcompat invocation and the
    `glyph-audit audit` subcommand.
    """
    audit.add_argument("--target", required=True,
                        help="Path to target font (.glyphspackage / .glyphs / .ttf / .otf)")
    audit.add_argument("--pair", action="append", default=[], type=_split_pair,
                        metavar="NAME=REFERENCE",
                        help="Map a target master name to a reference font. "
                             "Reference may be a TTF/OTF path, a Glyphs source path, "
                             "'Family-system', or any of those with an "
                             "'@axis=value[,axis=value]' suffix to pin a variable font. "
                             "Repeatable. Required unless --from-config is given.")
    audit.add_argument("--from-config", action="store_true", default=None,
                        help="Build pairs from [instances.NAME] sections in the "
                             "config file (default ~/.glyph-audit/config.toml; "
                             "override with --config). Adds to any --pair entries.")
    # All defaults below are sentinel-None so we can layer config defaults
    # on top before falling back to HARDCODED values.
    audit.add_argument("--output", default=None,
                        help="Markdown report path. Default: "
                             "'glyph-audit-report.md' (or 'glyph-audit-filtered.md' "
                             "if --filter is set). Overridable via [defaults].output in config.")
    audit.add_argument("--tolerance", type=float, default=None,
                        help="Advance-width tolerance in font units (default: 1.0).")
    audit.add_argument("--no-normalize-upm", action="store_true", default=None,
                        help="Compare raw advances without normalising to 1000 UPM.")
    audit.add_argument("--title", default=None,
                        help="Report title (default: 'Glyph Audit Report').")
    audit.add_argument(
        "--filter",
        choices=["all"] + sorted(COLOR_FILTERS.keys()),
        default=None,
        help="Restrict the target font to glyphs marked with a Glyphs colour. "
             "'yellow' = colour 3, 'light-green' = 4, 'green' = 5, "
             "'ready' = yellow OR light-green. Only effective for "
             ".glyphs / .glyphspackage target sources; ignored for TTFs.",
    )
    audit.add_argument(
        "--ai",
        choices=["claude", "openai", "gemini"],
        default=None,
        help="Add an AI-written health-check summary at the top of the report "
             "using the chosen provider. Requires the provider's SDK and an "
             "API key (in ~/.glyph-audit/config.toml or env vars).",
    )
    audit.add_argument(
        "--prompt",
        default=None,
        help="Path to a custom prompt template. Defaults to the bundled "
             "prompts/health_check.md inside the package.",
    )
    audit.add_argument(
        "--config",
        default=None,
        help="Path to config TOML (default: ~/.glyph-audit/config.toml). "
             "Holds API keys, [instances.NAME] map, and [defaults] section. "
             "See examples/config.toml.example for the schema.",
    )


def _run_audit(args, parser) -> int:
    """Execute the audit given already-parsed CLI args. `parser` is passed
    only for `parser.error(...)` — the runtime dispatch below happens to
    call it for the "at least one --pair" bootstrap case, and it wants to
    hit whichever parser matches the invocation form (top-level or
    `audit` subcommand)."""
    # Load [defaults] from the config file (silently missing is fine).
    try:
        config_defaults = load_defaults(args.config)
    except ConfigError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 2

    # Resolve each option: CLI value wins, then config default, then hard-coded.
    def resolved(arg_value, config_key, hardcoded):
        if arg_value is not None:
            return arg_value
        if config_key in config_defaults:
            return config_defaults[config_key]
        return hardcoded

    args.tolerance         = resolved(args.tolerance,         "tolerance",        HARDCODED["tolerance"])
    args.title             = resolved(args.title,             "title",            HARDCODED["title"])
    args.filter            = resolved(args.filter,            "filter",           HARDCODED["filter"])
    args.ai                = resolved(args.ai,                "ai",               None)
    args.prompt            = resolved(args.prompt,            "prompt",           None)
    args.no_normalize_upm  = bool(resolved(args.no_normalize_upm, "no_normalize_upm", False))
    args.from_config       = bool(resolved(args.from_config,  "from_config",      False))

    # Output path needs filter-awareness in its hard-coded fallback.
    filter_active = args.filter != "all"
    output_default = HARDCODED["output_filtered"] if filter_active else HARDCODED["output_no_filter"]
    args.output = resolved(args.output, "output", output_default)

    # Build the full pair list. CLI --pair entries first (no extra axes —
    # any '@axis=…' is already in the spec string and parsed by load_font).
    # Then [instances.NAME] entries from config.
    pair_list: list[tuple[str, str, dict[str, float]]] = [
        (name, spec, {}) for (name, spec) in args.pair
    ]
    if args.from_config:
        try:
            instances = load_instances(args.config)
        except ConfigError as e:
            print(f"FAIL: {e}", file=sys.stderr)
            return 2
        if not instances:
            path_for_msg = args.config or "~/.glyph-audit/config.toml"
            print(
                f"WARNING: --from-config given but no [instances.NAME] entries "
                f"found in {path_for_msg}.",
                file=sys.stderr,
            )
        pair_list.extend((inst.name, inst.ref, inst.axes) for inst in instances)

    if not pair_list:
        # First-run convenience: if the user has no config and no --pair,
        # bootstrap a copy of the bundled template into ~/.glyph-audit/config.toml
        # and exit successfully with instructions.
        default_config = os.path.expanduser("~/.glyph-audit/config.toml")
        if not args.config and not os.path.isfile(default_config):
            target = _bootstrap_default_config(default_config)
            if target:
                print(
                    f"\nNo config and no --pair/--from-config supplied.\n"
                    f"\nBootstrapped a starter config at: {target}\n"
                    f"\nNext steps:\n"
                    f"  1. Edit the file and uncomment / fill in your reference fonts under [instances.*].\n"
                    f"  2. Set 'from_config = true' under [defaults] to auto-pair them.\n"
                    f"  3. Re-run: glyph-audit --target <path-to-your-font>\n",
                    file=sys.stderr,
                )
                return 0

        parser.error("at least one --pair or --from-config (with [instances]) is required")

    target_filter = None
    filter_label = None
    if args.filter != "all":
        allowed_colors = COLOR_FILTERS[args.filter]
        names = sorted(GLYPHS_COLOR_NAMES[c] for c in allowed_colors)
        filter_label = f"{args.filter} (colour {' or '.join(str(c) for c in sorted(allowed_colors))} = {' / '.join(names)})"

    comparator = TieredComparator(
        tolerance_units=args.tolerance,
        normalize_upm=not args.no_normalize_upm,
    )

    results = []
    any_mismatch = False

    for master_name, ref_spec, ref_axes in pair_list:
        print(f"Loading target master {master_name!r} ← {args.target}", file=sys.stderr)
        try:
            target_view = load_font(args.target, master=master_name,
                                     label=f"{_basename(args.target)} {master_name}")
        except (FileNotFoundError, ValueError, RuntimeError) as e:
            print(f"  FAIL: {e}", file=sys.stderr)
            return 2

        ref_label_extra = f" @{','.join(f'{k}={v:g}' for k,v in ref_axes.items())}" if ref_axes else ""
        print(f"Loading reference {ref_spec!r}{ref_label_extra}", file=sys.stderr)
        try:
            reference_view = load_font(ref_spec, axes=ref_axes or None)
        except (FileNotFoundError, ValueError, RuntimeError) as e:
            print(f"  FAIL: {e}", file=sys.stderr)
            return 2

        # Build the target filter for this pair (target_view varies per pair
        # because each pair loads its own master). Filter only applies to
        # Glyphs sources where colours are populated.
        pair_filter = None
        if target_filter is None and args.filter != "all":
            allowed_colors = COLOR_FILTERS[args.filter]
            if not target_view.colors:
                print(
                    f"  WARNING: --filter {args.filter} requested but the target "
                    f"font has no colour metadata (source kind: "
                    f"{target_view.source_kind}). Ignoring filter for this pair.",
                    file=sys.stderr,
                )
            else:
                colors_map = target_view.colors
                pair_filter = lambda name, m=colors_map, a=allowed_colors: m.get(name) in a

        result = comparator.compare(
            target_view, reference_view,
            pair_label=master_name,
            target_filter=pair_filter,
            filter_label=filter_label if pair_filter else None,
        )
        results.append(result)

        c = result.counts()
        t1_mm = c["tier1"]["mismatch"]
        t2_mm = c["tier2"]["mismatch"]
        if t1_mm or t2_mm:
            any_mismatch = True
        print(
            f"  Tier1: {c['tier1']['match']}/{sum(c['tier1'].values())} matched, "
            f"{t1_mm} mismatched. "
            f"Tier2: {c['tier2']['match']}/{sum(c['tier2'].values())} matched, "
            f"{t2_mm} mismatched. "
            f"Tier3 internal: {c['tier3']['internal']}.",
            file=sys.stderr,
        )

    ai_summary_text: Optional[str] = None
    ai_provider_label: Optional[str] = None
    if args.ai:
        # Lazy import — keeps the AI subpackage off the import path for users
        # who never use --ai.
        from .ai import summarize, AIError
        ai_provider_label = args.ai
        try:
            print(f"Requesting AI summary from {args.ai} …", file=sys.stderr)
            ai_summary_text = summarize(
                results, args.ai,
                config_path=args.config,
                prompt_path=args.prompt,
            )
            print("AI summary received.", file=sys.stderr)
        except AIError as e:
            ai_summary_text = (
                f"_AI summary unavailable: {e}_"
            )
            print(f"  WARNING: AI summary failed — {e}", file=sys.stderr)

    write_markdown(
        results, args.output,
        title=args.title,
        ai_summary=ai_summary_text,
        ai_provider=ai_provider_label,
    )
    print(f"Wrote {args.output}", file=sys.stderr)
    return 1 if any_mismatch else 0


def main(argv: Optional[list[str]] = None) -> int:
    """Top-level dispatcher.

    Supports two invocation forms for backwards compatibility:
        glyph-audit --target … --pair …          (legacy — implicit `audit`)
        glyph-audit audit --target … --pair …    (explicit)
        glyph-audit proof {build|watch|serve|panel install} …

    Detection rule: if the first token is a known subcommand, treat it as
    such; otherwise inject `audit` at the front. This lets pre-0.2 scripts
    keep working without modification while `proof` gets a clean namespace.
    """
    from .cli_proof import add_proof_subparser, dispatch as proof_dispatch

    if argv is None:
        argv = sys.argv[1:]

    KNOWN_SUBCOMMANDS = {"audit", "proof", "coverage", "-h", "--help", "-V", "--version"}
    if argv and argv[0] not in KNOWN_SUBCOMMANDS and not argv[0].startswith("--"):
        # First token is a positional but not a subcommand — treat as
        # accidental typo; parse anyway so the user sees the usage.
        pass
    if argv and argv[0].startswith("--"):
        # Legacy: `glyph-audit --target …`. Inject `audit` so subparser
        # parsing works.
        argv = ["audit"] + argv

    parser = argparse.ArgumentParser(
        prog="glyph-audit",
        description=(
            "GlyphAudit — audit reports + live proofing pipeline for type designers.\n\n"
            "Subcommands:\n"
            "  audit   Legacy default. Compare a target font against reference pairs.\n"
            "  proof   Build + serve the proof web app (config from glyph-audit.toml)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="cmd")

    audit_parser = subparsers.add_parser(
        "audit",
        help="Coverage/width report against reference fonts.",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _build_audit_parser(audit_parser)
    audit_parser.set_defaults(_run=lambda a: _run_audit(a, audit_parser))

    add_proof_subparser(subparsers)

    coverage_parser = subparsers.add_parser(
        "coverage",
        help="Reference→target coverage gaps (what the reference has that the target lacks).",
        description=(
            "Inverse of the audit: list every codepoint and GSUB-reachable "
            "variant present in the reference but missing from the target. "
            "Exit code 1 when anything is truly absent (glyphs that exist "
            "but are unencoded/unlinked are warnings only)."
        ),
    )
    coverage_parser.add_argument(
        "--target", required=True,
        help="Path to target font (.glyphspackage / .glyphs / .ttf / .otf)",
    )
    coverage_parser.add_argument(
        "--pair", action="append", default=[], type=_split_pair,
        metavar="NAME=REFERENCE",
        help="Map a target master name to a reference font. Repeatable. "
             "Required unless --from-config is given.",
    )
    coverage_parser.add_argument(
        "--from-config", action="store_true", default=None,
        help="Build pairs from [instances.NAME] in the config file.",
    )
    coverage_parser.add_argument(
        "--output", default=None,
        help="Markdown report path (default: glyph-audit-coverage.md).",
    )
    coverage_parser.add_argument(
        "--emit-features", default=None, metavar="DIR",
        help="Also decompile each reference's GSUB into .fea files (one per "
             "pair, written to DIR) with rules rewritten into target glyph "
             "names. Rules referencing missing glyphs are skipped.",
    )
    coverage_parser.add_argument(
        "--config", default=None,
        help="Path to config TOML (default: ~/.glyph-audit/config.toml).",
    )
    coverage_parser.set_defaults(_run=lambda a: _run_coverage(a, coverage_parser))

    args = parser.parse_args(argv)
    if args.cmd is None:
        parser.print_help(sys.stderr)
        return 2

    if args.cmd == "proof":
        return proof_dispatch(args)
    # `_run` is set by _build_audit_parser via set_defaults above.
    return args._run(args)


def _run_coverage(args, parser) -> int:
    """Execute the `coverage` subcommand: reference→target gap report."""
    from .coverage import (
        ABSENT,
        build_feature_file,
        coverage_gaps,
        feature_table,
        glyph_matrix,
        reverse_gaps,
        write_markdown as write_coverage_markdown,
    )

    try:
        config_defaults = load_defaults(args.config)
    except ConfigError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 2

    from_config = args.from_config
    if from_config is None:
        from_config = bool(config_defaults.get("from_config", False))
    output = args.output or config_defaults.get(
        "coverage_output", "glyph-audit-coverage.md"
    )

    pair_list: list[tuple[str, str, dict[str, float]]] = [
        (name, spec, {}) for (name, spec) in args.pair
    ]
    if from_config:
        try:
            instances = load_instances(args.config)
        except ConfigError as e:
            print(f"FAIL: {e}", file=sys.stderr)
            return 2
        pair_list.extend((inst.name, inst.ref, inst.axes) for inst in instances)
    if not pair_list:
        parser.error("at least one --pair or --from-config (with [instances]) is required")

    results = []
    skipped: list[str] = []
    any_absent = False
    for master_name, ref_spec, ref_axes in pair_list:
        print(f"Loading target master {master_name!r} ← {args.target}", file=sys.stderr)
        try:
            target_view = load_font(args.target, master=master_name,
                                     label=f"{_basename(args.target)} {master_name}")
        except (FileNotFoundError, ValueError, RuntimeError) as e:
            # A pair whose master isn't in this source file is a skip, not
            # a failure: projects often split masters across files (e.g.
            # roman + italic sources) while the config lists them all.
            print(f"  WARNING: skipping pair {master_name!r}: {e}", file=sys.stderr)
            skipped.append(f"`{master_name}` — {e}")
            continue
        print(f"Loading reference {ref_spec!r}", file=sys.stderr)
        try:
            reference_view = load_font(ref_spec, axes=ref_axes or None)
        except (FileNotFoundError, ValueError, RuntimeError) as e:
            print(f"  FAIL: {e}", file=sys.stderr)
            return 2

        result = coverage_gaps(target_view, reference_view, pair_label=master_name)
        result.reverse = reverse_gaps(target_view, reference_view, pair_label=master_name)
        result.feature_rows = feature_table(target_view, reference_view)
        result.cp_matrix, result.var_matrix = glyph_matrix(target_view, reference_view)
        if args.emit_features:
            import os
            os.makedirs(args.emit_features, exist_ok=True)
            fea_text, stats = build_feature_file(target_view, reference_view)
            safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in master_name)
            fea_path = os.path.join(args.emit_features, f"{safe}.fea")
            with open(fea_path, "w", encoding="utf-8") as fh:
                fh.write(fea_text)
            result.fea_summary = (
                f"Wrote `{fea_path}` — {stats['features']} features, "
                f"{stats['rules']} rules copied, {stats['skipped']} skipped "
                f"(missing glyphs)."
            )
            if stats["skipped_features"]:
                result.fea_summary += (
                    " Fully skipped (no servable rules): "
                    + ", ".join(stats["skipped_features"]) + "."
                )
        results.append(result)
        cp_absent = sum(1 for g in result.codepoint_gaps if g.kind == ABSENT)
        var_absent = sum(1 for g in result.variant_gaps if g.kind == ABSENT)
        if cp_absent or var_absent:
            any_absent = True
        print(
            f"  {master_name}: {len(result.codepoint_gaps)} codepoint gaps "
            f"({cp_absent} absent), {len(result.variant_gaps)} variant gaps "
            f"({var_absent} absent).",
            file=sys.stderr,
        )

    write_coverage_markdown(results, output, skipped=skipped)
    print(f"Wrote {output}", file=sys.stderr)
    if not results and skipped:
        print("FAIL: every pair was skipped — nothing to report.", file=sys.stderr)
        return 2
    return 1 if any_absent else 0


def _basename(path: str) -> str:
    return os.path.basename(path.rstrip("/"))


def _bootstrap_default_config(target_path: str) -> Optional[str]:
    """Copy the bundled template to target_path. Returns the resolved path
    on success, or None if the template can't be located."""
    here = os.path.dirname(os.path.abspath(__file__))
    template = os.path.join(here, "examples", "config.toml.example")
    if not os.path.isfile(template):
        return None
    import shutil
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    shutil.copy(template, target_path)
    return target_path


if __name__ == "__main__":
    sys.exit(main())
