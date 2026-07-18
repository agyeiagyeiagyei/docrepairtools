# Testing plan — docrepair-glyph-audit

Living document describing how this package is tested before releases. Six
layers, cheapest first — pre-commit / CI can gate on L1–L4; L5 is a manual
checklist for humans; L6 is optional deep coverage before a PyPI publish.

Distribution: `docrepair-glyph-audit`.
Console script + Python module: `glyph-audit` / `GlyphAudit`.

---

## L1 — Fast unit tests (pytest, sub-second)

Run: `pytest tests/proof/`

| Module | Covers |
|---|---|
| `test_config.py` (31) | `validate_colors` (ints, strings, `"none"`, bool rejection), `load_project_config` discovery + walk-up + missing-file, all `[proof]` field validation, reference resolution (relative paths, `~`, absolute paths, unknown slot keys, empty references, `has_italic` helper), italic/roman source split. |
| `test_build_helpers.py` (14) | `output_paths_for` italic detection (case-insensitive, substring anywhere). `write_proof_config` schema, reference-copy idempotence, mtime-based re-copy, missing-reference tolerance, variable-slot handling, output-dir creation. |
| `test_server.py` (11) | Overlay resolution (output → dist fallback), path-escape defence, live-boot end-to-end (`_free_port` + threaded server), TTF + JSON content-type headers, 404 behaviour. |
| `test_cli.py` (8) | Legacy `--target` invocation still works (backcompat routing). Explicit `audit` subcommand. `proof -h` exits 0. Missing subcommand → 2. `proof build` requires config. All four proof subcommands (`build`, `watch`, `serve`, `panel`) present in help. |

Fixture: `conftest.py::tiny_source_factory` writes a `.glyphspackage` with a
configurable colour palette + optional component references + optional broken
features block. Uses Glyphs's on-disk filename convention (uppercase letters
get `_` suffix so case-insensitive filesystems don't collide).

Adding a test: use `tiny_source` for the default 8-glyph fixture,
`tiny_source_factory(name=…, glyphs=[…], features_block=…)` when you need a
different shape.

---

## L2 — Integration tests (pytest, seconds)

Run: `pytest tests/proof/test_build_integration.py`

Requires `fontc` on PATH — the whole file skips cleanly when it isn't.
`pip install docrepair-glyph-audit[proof]` pulls it in.

| Test | Assertion |
|---|---|
| `test_defaults_yellow_lightgreen` | Default `{"3","4"}` filter keeps yellow + light-green + essential glyphs; other colours filter out. |
| `test_all_colours_keeps_c_and_d` | Passing every colour keeps red-flagged (`c`) and uncoloured (`d`) glyphs. |
| `test_transitive_component_closure` | Yellow `/A` referencing uncoloured `/acomb`: build succeeds AND the closure pulled in the mark (fontc may rename to `uni0363` — we count total glyphs, not names). |
| `test_essential_glyphs_survive_narrow_filter` | `_notdef` + `space` present even under a colour filter that matches nothing. |
| `test_feature_filter_strips_broken_frac` | `frac` feature substituting to non-existent `.numr`/`.dnom` glyphs: filter demotes to `missing-glyphs` (empty lookup would otherwise crash fontc's FEA parser). |
| `test_manifests_written` | `available-chars.json` + `available-features.json` land in the output dir with the expected shape. |
| `test_italic_source_output_paths` | Italic source (name contains "Italic") emits `-italic`-suffixed TTF + manifests, not roman-named. |
| `test_missing_source_returns_false` | Missing source path → `build_font` returns False, doesn't raise. |

---

## L3 — Parity tests (pytest, one-shot)

Not committed yet — run manually before major refactors to guard against
behaviour drift. Snapshot the Velarium build:

```bash
# Baseline
glyph-audit proof build --colors 3,4 && cp -r proof-out/ /tmp/proof-out-baseline

# After the refactor
glyph-audit proof build --colors 3,4 && diff -r /tmp/proof-out-baseline proof-out
```

Byte-diff on TTFs is noisy (compilation timestamps, feature-order variance);
what matters is that cmap size, feature list, wght axis shape, and
`available-*.json` content stay stable.

---

## L4 — Web-app tests (Vitest, seconds)

Not committed yet — planned surface once we add Vitest to
`src/GlyphAudit/proof/webapp/package.json`:

| Test | Assertion |
|---|---|
| `proofConfig.loadProofConfig` | Mocks `fetch`: happy path → normalised config; 404 → fallback with reason; malformed JSON → fallback with reason. |
| `proofConfig.injectFontFaces` | Rendered `<style>` contains one `@font-face` per proof face + one per reference slot; family name quoted correctly; second call replaces first (idempotent). |
| `applyGlyphMarks` — missing | Chars outside `charSet` get `.missing-glyph`. HTML tags preserved. Entities skipped. |
| `applyGlyphMarks` — width | Chars whose codepoint is in `widthDeltas` get `.width-mismatch` with a `title` carrying the delta. |
| `applyGlyphMarks` — priority | Missing beats width: a codepoint absent from `charSet` gets `.missing-glyph` even when it's also in `widthDeltas` (you can't measure what isn't there). |
| `stripGlyphMarks` | Both marker classes strip cleanly; nested runs unwrap to the innermost text. |
| `Controls` reference dropdown | Empty `referenceFonts` → disabled with "(no references)". Populated → options rendered in order. |

Config slug (JS side) → build-side `_slugify` parity: guard test asserts that
`name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '')` on
common family names matches the Python side. Otherwise `widths-roman-<slug>`
URLs break silently.

---

## L5 — Manual smoke checklist (minutes)

Run after every phase merge, before publishing, and after any Glyphs.app
runtime change. Copy this list into a PR/release note as you tick.

1. `pytest tools/GlyphAudit/tests/` → all green.
2. `cd tools/GlyphAudit/src/GlyphAudit/proof/webapp && npm run build` → `dist/`
   produced, no warnings, JS bundle ≤ 170 KB.
3. `pip install -e tools/GlyphAudit` in a fresh venv → `which glyph-audit`
   reports the venv path; `glyph-audit --version` (once added) prints.
4. `glyph-audit proof build` from a project root with `glyph-audit.toml`:
   - `proof-out/` gets the TTF(s), `available-chars*.json`,
     `available-features*.json`, `proof-config.json`, `widths-*.json`
     per reference.
   - Reference TTFs copied into `proof-out/` (same-origin fetch).
5. `glyph-audit proof serve --no-browser` → `curl http://127.0.0.1:5173/`
   returns the packaged HTML shell; `/proof-config.json` returns from the
   output-dir overlay; `/Velarium-proof.ttf` returns with `Content-Type:
   font/ttf`.
6. Open the app in a browser:
   - Type a character not flagged in the proof subset (e.g., `ß` if not
     yellow) → red wavy underline on proof side only, clean on reference.
   - Type a character with divergent width vs the selected reference →
     amber-coloured glyph on proof side, hover shows `advance ±Nu vs
     reference`.
   - Toggle Italic → real italic outline renders (not synthetic — verify by
     shape, not just class list).
   - Toggle Bold → 100 % Bold master weight (not 60 %) — confirms the fvar
     post-fix.
7. In Glyphs.app:
   - `glyph-audit proof panel install` symlinks the panel; Option-click
     Script → Reload Scripts → **Script → Glyph Proof** opens the panel.
   - Colour checkboxes reflect the last-saved state; changing one persists
     to `~/.glyph-audit/proof-panel-state.json`.
   - **Launch proof window** → status transitions `idle → building → up to
     date`; log tail scrolls; browser opens (or "Open browser" works).
   - Edit + save a glyph in Glyphs → status flips to `building`; next tick
     back to `up to date`.
   - Kerning edit → same behaviour (watchdog covers `kerning.plist`).
   - **Stop proof window** → both processes exit; no orphaned Python in
     Activity Monitor after 5 s.
   - Width-audit section shows LSB / RSB delta columns when both sides
     carry sidebearing data; blank when either lacks it.
8. Close the panel → `~/.glyph-audit/proof-panel-state.json` retained;
   next open restores the same colour selection.

Missing a check counts as a smoke-test failure — file an issue rather
than fixing on the spot so the coverage gap is visible.

---

## L6 — Deep coverage before publish (optional, minutes)

Only run when cutting a PyPI release.

- **Wheel-build sanity**: `python -m build --wheel` in `tools/GlyphAudit/`
  succeeds; `unzip -l dist/*.whl | grep -E "dist/(index|assets)"` lists the
  packaged Vite bundle. Missing `assets/` means package-data glob is off.
- **Fresh-venv install**: `python -m venv /tmp/gapub-test && /tmp/gapub-test/bin/pip
  install dist/*.whl[proof]` → `/tmp/gapub-test/bin/glyph-audit proof serve
  --help` prints without touching the source tree.
- **Node-free end-to-end**: from an empty scratch dir, write a minimal
  `glyph-audit.toml` pointing at a `.glyphspackage` and a reference TTF →
  `glyph-audit proof build` → `glyph-audit proof serve --no-browser` →
  `curl` the app root. Confirms the shipped bundle boots without a Node
  runtime.
- **Playwright screenshot** (optional deep test): headless run of the
  packaged bundle, loads the page, waits for both `@font-face`s, snapshot
  the two panels for visual diff against baseline. Not gating on PRs;
  worth running before major web-app refactors.

---

## L7 — CI (GitHub Actions, ~5 min)

Two required jobs on every PR:

```yaml
test-python:
  strategy:
    matrix:
      python: ['3.10', '3.11', '3.12']
  steps:
    - setup-python@v5
    - pip install -e ".[proof]"
    - pytest tests/ --cov
```

```yaml
test-webapp:
  steps:
    - setup-node@v4  (node 20)
    - cd src/GlyphAudit/proof/webapp
    - npm ci
    - npm run lint
    - npm run build     # ensure dist/ is producible
    - npm test          # once L4 lands
```

Nightly `smoke-e2e` (non-gating):
- Boot the packaged server against a checked-in fixture project.
- Playwright headless screenshot; diff against baseline. New baseline
  requires a PR touching the snapshot file.

---

## What's NOT tested

- **fontc itself** — treated as an external dependency; version pin lives
  in `pyproject.toml`. If fontc's output shape changes between minor
  versions, our parity test in L3 catches it.
- **Glyphs.app runtime** — no headless Glyphs runner exists. The Glyphs
  panel is exercised only through the L5 manual checklist. Follow-up idea:
  extract the panel's non-UI helpers (state persistence, config discovery,
  subprocess management) into a module that can be unit-tested outside
  Glyphs.
- **`glyphsLib` parsing** — external dep; assume the `GSFont` API is stable
  across minor versions. Pinned in `[proof]` extras.
- **Real-world large sources** — the fixture is 8 glyphs. A nightly job
  running against a real production font (e.g., Velarium) as a smoke test
  would be a good extension; not gating.

---

## Where the tests live

```
tools/GlyphAudit/
  tests/
    __init__.py
    proof/
      __init__.py
      conftest.py                    # tiny_source_factory
      test_config.py                 # L1
      test_build_helpers.py          # L1
      test_server.py                 # L1
      test_cli.py                    # L1
      test_build_integration.py      # L2 (fontc-gated)
  src/GlyphAudit/proof/webapp/src/
    __tests__/                       # L4 (planned; not yet added)
      proofConfig.test.js
      applyGlyphMarks.test.js
      Controls.test.jsx
```
