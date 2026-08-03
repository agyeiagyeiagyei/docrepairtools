# Changelog

## 0.2.0

New tools, a renamed Glyphs menu, and a docs overhaul.

### New

- **Slant Glyphs panel** — slant selected glyphs with a live tweak loop:
  slanted-vs-reference outline preview in the panel, snapshot-based
  revert, extrema insert/remove for round glyphs with least-squares
  merges and handle balancing, and advance-width matching against the
  per-master reference on Apply.
- **`glyph-audit coverage` subcommand** — reports every codepoint and
  GSUB-reachable variant present in the reference but missing from your
  font (both directions), a per-feature matching table, and a yes/no
  glyph matrix. `--emit-features DIR` decompiles the reference's GSUB
  into `.fea` files rewritten in target glyph names. Exit 1 on true
  absences — usable as a Make/CI gate.
- **Coverage Check panel** — the same engine inside Glyphs: two-way
  gap check of the active font against config-pinned or uploaded
  references, with a searchable missing-glyph list and one-click
  report/`.fea` export.

### Changed

- **DocRepair Tools menu renames** — Glyph Audit → **Width Audit**,
  Make Proof → **Proof Builder**, Coverage → **Coverage Check**
  (Slant Glyphs is new). Run `glyph-audit proof panel install` after
  upgrading; the installer removes the old menu links.
- **Proof web view** — page title is now `Docrepair Proof: <family>`.
- **README/docs** — Glyphs-first, image-forward rewrite with panel and
  app screenshots.

### Notes for upgraders

- The Glyphs menu entries changed names — reinstall the panels
  (`glyph-audit proof panel install`, then Option-click
  Script → Reload Scripts).
- Coverage Check's fix actions (assign Unicode / create glyph /
  create variant) never copy outlines from the reference; empty glyphs
  are created at the reference's advance width for you to draw.

## 0.1.0

First release: tiered coverage/width audit CLI, proof pipeline with
side-by-side browser compare, and the Glyphs.app panels (width audit,
proof launcher).
