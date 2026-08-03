# Configuration

The config file lives at `~/.glyph-audit/config.toml` by default. On first run with a `--target` but no `--pair` / `--from-config` and no existing config, the tool bootstraps this file from the bundled template — see [`examples/config.toml.example`](../src/GlyphAudit/examples/config.toml.example).

Override location with `--config PATH`. The file holds API keys; treat it like any other secrets file.

## Sections

| Section | Purpose |
|---|---|
| `[defaults]` | CLI flag defaults. Skip retyping common flags. Any explicit CLI flag overrides values here. |
| `[instances.NAME]` | Reusable target-master ↔ reference map. Consumed when `--from-config` is set (or `from_config = true` under `[defaults]`). |

Both are optional. Set only what you use.

## `[defaults]`

Keys mirror CLI flags (hyphens become underscores, e.g. `--no-normalize-upm` → `no_normalize_upm`).

```toml
[defaults]
filter           = "ready"
from_config      = true
title            = "MyTypeface Audit"
ai               = "claude"
output           = "glyph-audit-report.md"
tolerance        = 1.0
prompt           = "~/.glyph-audit/prompts/strict.md"
no_normalize_upm = false
```

## `[instances.NAME]`

Each entry maps one of the target font's master names (case-insensitive) to a reference. `NAME` should match the master name in your Glyphs file (`Regular`, `Bold`, `Display`, …).

```toml
[instances.Regular]
ref  = "/path/to/Reference-Regular.ttf"

[instances.Bold]
ref  = "Inter[wght].ttf"
axis = { wght = 700 }
```

### Reference forms

The `ref` value can be any of:

1. **Static TTF / OTF on disk**
   ```toml
   ref = "/Users/me/fonts/Helvetica.ttf"
   ```

2. **Variable font on disk + axis pin**
   ```toml
   ref  = "/Users/me/fonts/Inter[wght].ttf"
   axis = { wght = 400 }
   ```

3. **System-installed font** (macOS / Windows / Linux)
   ```toml
   ref = "Helvetica-system"           # Regular implied
   ref = "Helvetica-Bold-system"      # hyphen-form
   ref = "Helvetica Bold-system"      # space-form (also works)
   ```

4. **Glyphs source** (`.glyphspackage` / `.glyphs`)
   ```toml
   ref = "/Users/me/fonts/MyOldVersion.glyphspackage"
   ```
   Caveat: with multiple masters in the source, the tool currently picks the first one regardless of which target master is paired with it. Workaround: export to TTF first.

5. **Google Font** — download once, then reference like any TTF or VF:
   ```toml
   # Static download
   ref = "/Users/me/fonts/Inter/static/Inter-Regular.ttf"

   # Variable download (most modern Google Fonts ship as VFs)
   ref  = "/Users/me/fonts/Inter/Inter[opsz,wght].ttf"
   axis = { wght = 400 }

   # Or install via Font Book and treat as system
   ref = "Inter-Regular-system"
   ```
   `gftools download Inter` (from the `gftools` pip package) automates the download into `~/Library/Fonts` on macOS.

## Project-local config

Pass `--config PATH` to point at a project-local file:

```bash
glyph-audit --target sources/MyTypeface.glyphspackage --config .glyph-audit.toml
```

The user-level default at `~/.glyph-audit/config.toml` sits outside any repo, so it can't be accidentally committed.
