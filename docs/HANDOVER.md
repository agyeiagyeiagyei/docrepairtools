# DocRepair Tools — Project Handover

Last updated: 2026-07-23

A working reference for anyone picking up this project. Covers what it is,
how the pieces fit, what's done, what's outstanding, and how to run / release
it. Pairs with [`TESTING.md`](../TESTING.md) (test strategy) and
[`docs/release.md`](release.md) (publish mechanics).

---

## 1. What this is

Tooling for the **Google Fonts docrepair project** — building *metrics-compatible
replacement fonts* that can stand in for an original document face without
reflowing the documents that used it. The recurring need is **"match-but-not-quite"**:
same advances and coverage as a reference, distinct outlines.

The toolkit ships as one Python distribution with three surfaces:

| Surface | What it does | Entry point |
|---|---|---|
| **Audit CLI** | Tiered coverage/width comparison of a target vs reference fonts → Markdown report | `glyph-audit --target … --pair …` |
| **Proof pipeline** | Subset a Glyphs source to a proof TTF, serve a side-by-side browser compare, watch+rebuild | `glyph-audit proof {build,watch,serve}` |
| **Glyphs.app panels** | Live width-audit table + edit-view reference overlay; proof-pipeline launcher | Script → DocRepair Tools → {Glyph Audit, Make Proof} |

### Naming layers (they intentionally differ)

| Layer | Name |
|---|---|
| GitHub repo | `docrepairtools` |
| PyPI distribution | `docrepair-tools` |
| Console script | `glyph-audit` |
| Import package | `GlyphAudit` |
| Glyphs menu | DocRepair Tools ▸ Glyph Audit · Make Proof |

The distribution is the umbrella brand; the console script and import package
stay short so existing code, `glyph-audit.toml` configs, and `glyph-audit …`
commands never had to change through the renames.

---

## 2. Repository map

```
docrepairtools/
├── pyproject.toml              # dist=docrepair-tools, extras: glyphs/proof/ai/all
├── README.md                   # user-facing intro (has the two screenshots)
├── TESTING.md                  # 7-layer test plan (L1–L7)
├── docs/
│   ├── HANDOVER.md             # ← this file
│   ├── release.md              # PyPI publish + trusted-publisher setup
│   ├── configuration.md        # ~/.glyph-audit/config.toml schema
│   ├── cli.md, concepts.md, ai-summary.md
│   └── screenshots/            # proof-web.png, width-audit-panel.png (README refs)
├── .github/workflows/publish.yml  # tag-driven build+publish (builds webapp bundle first)
├── tests/proof/                # 131 tests
└── src/GlyphAudit/
    ├── cli.py / cli_proof.py   # argparse: `audit` (legacy default) + `proof` subtree
    ├── model.py                # FontView, COLOR_FILTERS, feature-suffix parsing
    ├── comparator.py           # TieredComparator (T1 codepoint / T2 variant / T3 internal)
    ├── loaders.py              # TTF / Glyphs-source loaders (system-font path dormant)
    ├── report.py, defaults.py, instances.py
    ├── ai/                     # optional --ai summaries (Claude/OpenAI/Gemini)
    └── proof/                  # ── proofing subsystem ──
        ├── config.py           # glyph-audit.toml schema ([proof], [references])
        ├── build.py            # fontc subset build; the engine (see §4)
        ├── server.py           # stdlib server overlaying output_dir on the webapp bundle
        ├── webapp/             # React/Vite app; prebuilt dist/ ships in the wheel
        └── panel/              # Glyphs.app panels (see §5)
```

### The audit ↔ proof boundary (deliberate)

The proof vertical is **severable from the audit core**. `proof/build.py`'s only
in-package import is `.config`; it never touches `model`/`comparator`/`loaders`.
The panels were split along the same line:

- `panel/audit_common.py` — audit-side helpers (FontView bridge, reference picker,
  TTFont cache, state I/O). **Zero `proof.config` imports** (AST-verified in the
  boundary commit).
- `panel/proof_panel.py` — carries the proof-only helpers (project-config
  discovery, subprocess plumbing) alongside its sole consumer.
- `panel/common.py` — now just a backwards-compat shim re-exporting from both.

This means "extract the audit panel as a standalone package" is file-moving, not
surgery. Do it only if a real second consumer appears.

---

## 3. Status — done vs outstanding

### ✅ Done
- Full proof subsystem: config schema, fontc subset build, width manifests,
  italic post-fixes, stdlib overlay server, prebuilt React webapp.
- CLI: `audit` (back-compat flat invocation still works) + `proof {build,watch,serve,panel install}`.
- Two Glyphs panels installed as a **DocRepair Tools submenu**; installer removes
  superseded top-level links on upgrade.
- Edit-view **reference overlay**: outline + node circles (composites decomposed),
  LSB/advance metric-delta bands (italic-slanted), pairing label, per-master
  reference resolution following the actively-edited master.
- **Pin-to-master** config writer (surgical TOML edits, comment-preserving, 13 tests).
- Full **color-filter palette** in the audit dropdown (12 colours + ready + no-colour).
- Renames complete: repo → `docrepairtools`, dist → `docrepair-tools`. GitHub
  description updated.
- **131 tests green.** Wheel + sdist build clean (`docrepair_tools-0.1.0`), pass
  `twine check --strict`, fresh-venv install verified (webapp resolves in site-packages).

### ⚠️ Outstanding
1. **Not on PyPI yet.** `pip install docrepair-tools` → 404. First upload is manual
   (trusted-publisher CI can only attach after a project exists):
   ```bash
   cd ~/Documents/docrepairtools && twine upload dist/docrepair_tools-0.1.0*
   ```
   Needs a PyPI API token. After it lands, configure the trusted publisher
   (see `docs/release.md`; project=`docrepair-tools`, repo=`docrepairtools`,
   workflow=`publish.yml`, environment=`pypi`) and tag `v0.1.0` so CI takes over
   future releases.
2. **No release tag.** `git tag -l` is empty.
3. **LSB/RSB delta columns hidden in the Width Audit table.** The RSB math
   (advance − LSB − glyf bbox width, includes off-curve control points) diverges
   from Glyphs's on-curve `layer.RSB`. Fields still populated on `FontView`;
   columns just not rendered. Fix = compute on-curve bbox in the TTF loader to
   match Glyphs's convention, then re-add the columns. The overlay's advance/LSB
   bands are a separate, working code path.
4. **Web L4 tests not written.** `TESTING.md` L4 (Vitest for the React app —
   `proofConfig`, `applyGlyphMarks`, `Controls`) is planned but not implemented.
   Adds a Node dev-dep to the webapp; do before relying on the web app in CI.
5. **System-font reference lookup dormant.** Removed from the panel dropdown
   (Apple's Verdana ≠ Microsoft's GSUB coverage). `matching.py` + its 46 tests
   stay, free-standing, if it's ever re-enabled.
6. **`src/GlyphAudit/proof/webapp/node_modules/` in the source tree.** Harmless —
   gitignored and excluded from the wheel via `packages.find` exclude — but a
   stray `flatted/python/flatted.py` shows up in tree walks. Don't be alarmed.

---

## 4. The build engine (`proof/build.py`)

The heart of the proof pipeline. `build_font(source, output_dir, basename, colors)`:

1. **Filter** `.glyph` files by Glyphs colour label (default yellow=3 + light-green=4).
2. **Transitive component closure** — pull in un-tagged components that tagged
   glyphs reference (accent bases, `currencybar`, etc.). Without this `fontc`
   panics on missing component refs. These enter the TTF but not the cmap.
3. **Strip broken features** — any GSUB rule referencing a dropped glyph is
   removed so the FEA still compiles; empty lookups get demoted so `fontc` doesn't
   choke on `lookup FOO {} FOO;`.
4. **Compile** with `fontc` → variable TTF.
5. **Italic post-fixes**: set `USE_TYPO_METRICS` (line-height parity with roman);
   relabel the `wght` axis max 900→700 so `font-weight:700`/Bold lands on the
   Bold-Italic master instead of interpolating ~60% toward it.
6. **Emit manifests**: `available-chars/features*.json`, `proof-config.json`,
   `widths-*.json` (per-reference advance deltas the web app paints amber).

`fontc` is a hard runtime dep (in the `proof` extra); `cmd_build` pre-flights it
with an actionable error.

---

## 5. Glyphs.app panels (`proof/panel/`)

Installed via `glyph-audit proof panel install` → symlinks into
`~/Library/Application Support/Glyphs 3/Scripts/DocRepair Tools/`. A folder there
renders as a **submenu** — no plugin bundle needed. See README's "Scripts vs
Plugins" reasoning; short version: Scripts keeps the panels versioned with the
pip package (one `pip install -U` updates everything).

**Two Glyphs-specific gotchas future-you will hit:**

- **`sys.modules` survives "Reload Scripts".** Each panel purges cached
  `GlyphAudit*` modules on run so edits take effect without an app restart. The
  open-panel instance registry lives on `builtins` (not a class attr) so
  toggle-to-close survives the purge.
- **NSTimer needs an NSObject target.** A plain Python callable silently never
  fires. The proof panel streams subprocess output via a daemon thread +
  `PyObjCTools.AppHelper.callAfter` instead (see the long comment in
  `proof_panel.py::ProofPipeline`).

Panels depend on Glyphs's embedded Python having `GlyphAudit` importable — the
editable/pip install on PATH handles that; each panel also has an inline
sys.path bootstrap that walks up to the checkout's `src/` as a fallback.

---

## 6. How to run

### As a consumer (Velarium is the reference consumer)
A font project drops a `glyph-audit.toml` at its root (see Velarium's for a
worked example: family name, sources list, colour filter, `[references]`), then:

```bash
glyph-audit proof build          # yellow-subset TTFs + manifests → proof-out/
glyph-audit proof serve --watch  # build + browser compare at localhost:5173, live rebuild
glyph-audit --target sources/Foo.glyphspackage --pair Regular=Ref.ttf   # audit report
```

Export the yellow-tagged glyphs as variable TTFs (what "make proof app" produces):
```bash
glyph-audit proof build --colors 3 --source A.glyphspackage --source "A Italic.glyphspackage"
# → proof-out/A-proof.ttf + A-proof-italic.ttf (wght variable, per-master instances)
```

### As a developer of the tool
```bash
cd ~/Documents/docrepairtools
pip install -e .            # editable; console script on PATH
python -m pytest tests/     # 131 tests, fontc-gated ones skip if fontc absent
cd src/GlyphAudit/proof/webapp && npm ci && npm run build   # rebuild the shipped bundle
```

Local machine state: editable-installed from `~/Documents/docrepairtools`; Glyphs
panels symlinked and live.

---

## 7. Release checklist (from `docs/release.md`)

1. `cd src/GlyphAudit/proof/webapp && npm run build` — refresh the shipped bundle
   (CI does this too; the wheel is useless without it).
2. Bump `version` in `pyproject.toml`.
3. `rm -rf dist && python -m build && twine check --strict dist/*`.
4. **First ever upload only:** `twine upload dist/*` with a PyPI token, then
   configure the trusted publisher.
5. Thereafter: commit, `git tag vX.Y.Z`, `git push --tags` → CI builds + publishes
   via OIDC. Tags with a `-suffix` (e.g. `v0.1.0-rc1`) build but don't upload.

---

## 8. Related: the Velarium consumer

`~/Documents/Velarium` — the SorkinType/Velarium typeface — is the primary
real-world consumer and where this tooling was grown. Relevant state:

- Uses the tool via its root `glyph-audit.toml`; `make proof-font` / `proof-app`
  delegate to `glyph-audit`.
- Three type-review passes filed as GitHub issues (Cyrillic, Armenian, Greek);
  ~49 open issues. Review-filing conventions live in Velarium's `CLAUDE.md`
  (15 rules) — specimen rendering, per-glyph issues, verbatim reviewer quotes.
- Review specimens stored on per-review branches: `origin/{cyrillic-review,
  Armenian,greek-review}` (image storage, not for merge).
- The Greek review (Irene Vlachou, 35 issues #63/#69–#102) is the most recent.

None of that is required to work on the tool — it's context for why the audit +
proof features exist in the shape they do.
