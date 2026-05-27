# CLI reference

```
glyph-audit --target PATH [--pair NAME=REF | --from-config] [options]
```

The `glyph-audit` console script and `python -m GlyphAudit` are equivalent.

## Options

| Flag | Default | Notes |
|---|---|---|
| `--target PATH` | required | Font to audit (`.glyphspackage` / `.glyphs` / `.ttf` / `.otf`). |
| `--pair NAME=REF` | repeatable | Map a target master name to a reference. `REF` may be a TTF/OTF path, a Glyphs source path, `Family-system`, or any of those with an `@axis=value[,axis=value]` suffix to pin a variable font. Required unless `--from-config` is given. |
| `--from-config` | off | Build pairs from `[instances.NAME]` entries in the config file. Adds to any explicit `--pair` flags. |
| `--output PATH` | `glyph-audit-report.md` (or `glyph-audit-filtered.md` when `--filter` is active) | Markdown output path. |
| `--tolerance N` | `1.0` | Max acceptable advance-width delta. Per-1000-UPM normalised by default; raw font units when `--no-normalize-upm` is set. |
| `--no-normalize-upm` | off | Compare raw advances instead of per-1000-UPM-normalised values. |
| `--title TEXT` | `"Glyph Audit Report"` | Top-level report heading. |
| `--filter NAME` | `all` | Restrict the target font to glyphs marked with a Glyphs colour. Only effective for `.glyphs` / `.glyphspackage` target sources; ignored for TTFs. |
| `--ai PROVIDER` | off | Add an AI-written health-check summary at the top of the report. Provider is one of `claude`, `openai`, `gemini`. See [ai-summary.md](ai-summary.md). |
| `--prompt PATH` | bundled default | Custom prompt template for the AI summary. |
| `--config PATH` | `~/.glyph-audit/config.toml` | Override config file location. |

## Filter values

| `--filter` | Glyphs colour index | Conventional meaning |
|---|---|---|
| `yellow` | 3 | Ready for testing |
| `light-green` | 4 | Passed inspection |
| `green` | 5 | Production-ready |
| `ready` | 3 OR 4 | Either of the above |
| `all` *(default)* | — | No filtering |

## Exit codes

- `0` — all matched within tolerance, or first-run bootstrap created config
- `1` — at least one mismatch
- `2` — load error (target / reference / config unreadable)

## Examples

### Two TTF references

```bash
glyph-audit --target sources/MyTypeface.glyphspackage \
            --pair Regular=sources/reference/Reference-Regular.ttf \
            --pair Bold=sources/reference/Reference-Bold.ttf
```

### Variable font reference

```bash
glyph-audit --target sources/MyTypeface.glyphspackage \
            --pair Regular="Inter[wght].ttf@wght=400" \
            --pair Bold="Inter[wght].ttf@wght=700"
```

### System-installed font

```bash
glyph-audit --target MyFont.ttf \
            --pair Default="Helvetica-Bold-system"
```

### Cross-format, multi-pair

Run the same master against two different references in one report:

```bash
glyph-audit --target sources/MyTypeface.glyphspackage \
            --pair Bold=sources/reference/Reference-Bold.ttf \
            --pair Bold-vs-system="Helvetica Bold-system"
```

### Yellow-only filter, with AI summary

Assuming `[providers.claude]` and `[instances.*]` are set up in config:

```bash
glyph-audit --target sources/MyTypeface.glyphspackage \
            --filter yellow \
            --ai claude \
            --from-config
```

## Recipes

### One-line `make audit` target

Once `[defaults]` and `[instances.*]` are in `~/.glyph-audit/config.toml`, this is enough to wire the audit into a typeface project's Makefile:

```make
TARGET ?= sources/MyTypeface.glyphspackage
audit:
	-glyph-audit --target "$(TARGET)"
```

```bash
make audit                                                # default target
make audit TARGET=sources/MyTypeface-working.glyphspackage
```

The leading `-` tells `make` to ignore `glyph-audit`'s exit code 1 (the "I found mismatches" signal — expected during proofing, not a build failure).

### Shell alias

For projects without a Makefile, a shell function gives you the same ergonomics:

```bash
# in ~/.zshrc or ~/.bashrc
audit() {
  glyph-audit --target "${1:-sources/MyTypeface.glyphspackage}"
}
```

```bash
audit                                                # uses the default
audit sources/MyTypeface-working.glyphspackage
```

### Per-project config

Working on multiple typefaces? Drop a `.glyph-audit.toml` alongside the source and point `--config` at it instead of the global file:

```bash
glyph-audit --target sources/MyTypeface.glyphspackage --config .glyph-audit.toml
```

The per-project file accepts the same `[defaults]` / `[instances.*]` / `[providers.*]` sections. Just don't commit it if it contains API keys.

## Precedence

Resolved in this order, highest first:

1. Explicit CLI flag.
2. `[defaults].KEY` in the config file.
3. Built-in fallback baked into the tool.

So setting `filter = "ready"` under `[defaults]` makes every run a ready-filtered run, but `--filter all` on the CLI still wins for one-off unfiltered runs.

For the config schema see [configuration.md](configuration.md).
