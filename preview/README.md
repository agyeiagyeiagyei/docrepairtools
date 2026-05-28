# GlyphAudit Preview

A side-by-side proof viewer for in-development typefaces. Compares the proof font built from your `.glyphspackage` source against a reference font of your choice — same font size, line-height, letter-spacing on both sides, with kerning explicitly suppressed so identical glyph advances produce identical line wraps.

## What it gives you

- **Live editable text on both panels.** Type in the left (proof) panel; the right panel mirrors verbatim.
- **OpenType feature dropdown** populated from the compiled proof font's GSUB, with a colour-coded status per tag (`compiled` / `missing-glyphs` / `disabled` / `needs-environment`). Clicking applies the feature to the current selection on both panels; "Clear features in selection" reverses it.
- **Missing-glyph indicator** — codepoints not in the proof font's yellow/light-green subset get a red wavy underline on the left panel only, so you can see at a glance what hasn't been drawn yet.
- **Headline + body size sliders, line-height, tracking, Bold/Italic toggles** — the usual proofing controls.

## Architecture

```
preview/
├── build.py            ← Python build script: reads your .glyphspackage,
│                         filters to ready glyphs, runs fontc, writes the
│                         proof font and three JSON manifests into public/.
├── package.json        ← Vite + React app.
├── index.html
├── src/                ← React components
└── public/             ← Build outputs (everything except static assets is
                          generated; see .gitignore):
    ├── proof-font.ttf            (built by build.py)
    ├── proof-config.json         (runtime config — project name, font
    │                              family, reference fonts to load via
    │                              @font-face)
    ├── available-chars.json      (codepoint manifest)
    ├── available-features.json   (per-feature status)
    └── ref/                      (copied reference font TTFs)
```

The Python script and the React app communicate **only** through files in `public/`. Run them in either order — the app polls every 3 s and picks up new builds without a reload.

## Install (one-time)

```bash
cd preview
npm install
```

The `glyph-audit` Python package needs to be on your `$PATH` (or installed in the same Python that will run `build.py`) so the comparator's helpers are importable. From the repo root:

```bash
pip install -e ".[glyphs]"
```

Plus [`fontc`](https://github.com/googlefonts/fontc) for the proof font compilation:

```bash
pip install fontc
```

## Use

Run the build from anywhere — output paths are anchored to `preview/public/` regardless of CWD:

```bash
# Minimal: project name comes from the source filename stem,
# proof family becomes "<Name> Proof", references read from any
# [instances.*] entries in ~/.glyph-audit/config.toml.
python /path/to/GlyphAudit/preview/build.py \
    --source sources/MyTypeface.glyphspackage

# Explicit reference fonts (repeat --reference for each style):
python /path/to/GlyphAudit/preview/build.py \
    --source sources/MyTypeface.glyphspackage \
    --name "MyTypeface" \
    --reference Verdana:regular=sources/reference/VERDANA.TTF \
    --reference Verdana:bold=sources/reference/VERDANAB.TTF \
    --reference Verdana:italic=sources/reference/VERDANAI.TTF \
    --reference Verdana:boldItalic=sources/reference/VERDANABI.TTF

# Watch the source and rebuild on every .glyph save:
python /path/to/GlyphAudit/preview/build.py \
    --source sources/MyTypeface.glyphspackage --watch
```

Then start the dev server in another terminal:

```bash
cd /path/to/GlyphAudit/preview
npm run dev
```

Open the URL it prints (usually `http://localhost:5173`). The app re-fetches `proof-config.json` and the manifests every 3 seconds, so as you re-run the build (or leave `build.py --watch` running), the preview updates without a manual reload.

## CLI reference

| Flag | Default | Notes |
|---|---|---|
| `--source PATH` | required | Path to the typeface `.glyphspackage` / `.glyphs` source. |
| `--name NAME` | source filename stem | Project name. Used as the default headline and to derive the proof font family. |
| `--proof-family FAMILY` | `"<NAME> Proof"` | Override the proof font family-name (the `@font-face` family the left panel uses). |
| `--reference FAMILY[:STYLE]=PATH` | repeatable | Reference font(s) for the comparison panel. Style is `regular` (default), `bold`, `italic`, or `boldItalic`. Files are copied into `public/ref/` and exposed via `@font-face` at runtime. |
| `--headline TEXT` | project name | Default text in the headline editable. |
| `--body TEXT` | bundled pangrams | Default text in the body editable. |
| `--watch` | off | Rebuild on every `.glyph` file save (requires `watchdog`). |

If `--reference` is omitted entirely, the script falls back to any `[instances.*]` entries already present in `~/.glyph-audit/config.toml` — the same map the CLI's `--from-config` uses. Master names are matched to styles by substring (`regular` / `bold` / `italic`), so `[instances.Regular]` becomes the regular style, `[instances.Bold Italic]` becomes `boldItalic`, etc.

## Why kerning is off by default

If both fonts had identical advance widths but different kerning, the same paragraph would wrap at different positions on the two sides, defeating the line-by-line comparison the app exists for. The CSS sets `font-kerning: none` plus `'kern' 0` in `font-feature-settings`, and feature-application spans inherit kern-off. The cumulative line width on each side is then purely a function of glyph advances — which is what the build script's audit is designed to track.

If you specifically want to inspect kerning, applying `kern` from the Features dropdown will re-enable it for the selected text on both panels.

## Reference font licensing

The build script copies whatever you point `--reference` at into `public/ref/`. **Don't commit those copies if the source font is proprietary** (Microsoft's Verdana, Apple's Helvetica, etc.). `public/ref/` is in `.gitignore` for that reason. Use Google Fonts, Open Foundry releases, or any other OFL/MIT/Apache-licensed family if you want a permanently-shipping reference.
