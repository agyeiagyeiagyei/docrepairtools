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
| `--config PATH` | `~/.glyph-audit/config.toml` | Override config file location. |

## Filter values

One value per Glyphs palette colour, plus two convenience aliases:

| `--filter` | Glyphs colour index | Conventional meaning |
|---|---|---|
| `red` | 0 | |
| `orange` | 1 | |
| `brown` | 2 | |
| `yellow` | 3 | Ready for testing |
| `light-green` | 4 | Passed inspection |
| `green` | 5 | Production-ready |
| `light-blue` | 6 | |
| `blue` | 7 | |
| `purple` | 8 | |
| `pink` | 9 | |
| `light-gray` | 10 | |
| `gray` | 11 | |
| `ready` | 3 OR 4 | Yellow or light-green — the project convention for "flagged for proofing" |
| `no-colour` | — | Glyphs with no colour label at all |
| `all` *(default)* | — | No filtering |

## Coverage subcommand

`glyph-audit coverage` answers "**is anything the reference covers missing from my font?**" — the gaps that make documents fall back to another font.

```
glyph-audit coverage --target PATH [--pair NAME=REF | --from-config]
                     [--output PATH] [--emit-features DIR] [--config PATH]
```

The markdown report (default `glyph-audit-coverage.md`) contains, per master/reference pair:

- **Missing codepoints** — grouped by Unicode block, with character names and reference glyphs. **Missing feature variants** — grouped by feature tag. Both listed in *both directions* (reverse is informational).
- **Feature matching table** — per OpenType feature: reference rules vs rules the target can serve (`full` / `partial` / `missing`).
- **Full yes/no matrix** — every codepoint and variant in the union of both fonts, `glyph | target | reference`.

Gap classification: **absent** (fails the run), `unencoded-in-target` (glyph exists but has no Unicode value — warning), `present, not feature-linked` (warning). Exit code `1` on true absences, else `0` — usable as a Make/CI gate.

`--emit-features DIR` also writes one `.fea` per pair: the reference's GSUB decompiled and rewritten into *target* glyph names, ready to review and import into Glyphs. Rules referencing glyphs the target lacks are skipped and counted. Kerning (GPOS) is reported but not copied.

Pairs whose master isn't in the target source are skipped with a warning, so projects with masters split across files (roman + italic sources) can run `--from-config` per file.

The same checks run inside Glyphs.app via **Script → DocRepair Tools → Coverage Check**, against the active font, with a searchable missing-glyph list and one-click report/`.fea` export.

## Proof subcommands

`glyph-audit proof` compiles a colour-filtered subset of a Glyphs source into a proof TTF and serves a side-by-side browser comparison against configured reference fonts. It reads `glyph-audit.toml` from the project root (override with `--config`) and requires the `proof` extra: `pip install "docrepair-tools[proof]"`.

| Command | What it does |
|---|---|
| `proof build` | One-shot compile of all sources → proof TTFs + manifests in the output dir. |
| `proof watch` | Build once, then rebuild automatically on source changes. |
| `proof serve` | Build, then serve the web app at `http://127.0.0.1:5173` and open it in a browser. |
| `proof panel install` | Symlink the Glyphs.app panels (Width Audit, Proof Builder, Slant Glyphs, Coverage Check) into Glyphs 3's Scripts folder. |

Flags shared by `build` / `watch` / `serve`:

| Flag | Default | Notes |
|---|---|---|
| `--config PATH` | discovered from CWD | Path to `glyph-audit.toml`. |
| `--source PATH` | from config | Override the config's source list. Repeatable. |
| `--colors LIST` | from config | Comma-separated Glyphs colour keys (`0`–`11` or `none`). Overrides `[proof].colors`. |

`proof serve` additionally accepts:

| Flag | Default | Notes |
|---|---|---|
| `--host ADDR` | `127.0.0.1` | Bind address. |
| `--port N` | `5173` | Port (matches Vite's dev-server default). |
| `--no-browser` | off | Skip auto-opening the browser on start. |
| `--no-build` | off | Skip the initial build (output dir already fresh). |
| `--watch` | off | Also spawn a watcher so source edits rebuild live. |

Example — export the yellow-tagged glyphs as variable proof TTFs and compare live in the browser:

```bash
glyph-audit proof build --colors 3 --source A.glyphspackage --source "A Italic.glyphspackage"
glyph-audit proof serve --watch
```

## Exit codes

For the audit invocation:

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

### Yellow-only filter, config pairs

Assuming `[instances.*]` and `filter = "yellow"` are set up in config:

```bash
glyph-audit --target sources/MyTypeface.glyphspackage --from-config
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
make audit TARGET=sources/MyTypeface-Italic.glyphspackage
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
audit sources/MyTypeface-Italic.glyphspackage
```

### Per-project config

Working on multiple typefaces? Drop a `.glyph-audit.toml` alongside the source and point `--config` at it instead of the global file:

```bash
glyph-audit --target sources/MyTypeface.glyphspackage --config .glyph-audit.toml
```

The per-project file accepts the same `[defaults]` / `[instances.*]` sections.

## Precedence

Resolved in this order, highest first:

1. Explicit CLI flag.
2. `[defaults].KEY` in the config file.
3. Built-in fallback baked into the tool.

So setting `filter = "ready"` under `[defaults]` makes every run a ready-filtered run, but `--filter all` on the CLI still wins for one-off unfiltered runs.

For the config schema see [configuration.md](configuration.md).
