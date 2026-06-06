# Glyphs.app integration

Companion scripts that run inside Glyphs's Python environment and use the
`GlyphAudit` library directly.

## Width Audit Panel

[`width_audit_panel.py`](width_audit_panel.py) — a floating Vanilla window
that lists every width mismatch between the open Glyphs font and a
reference, refreshing live as you edit. Stays above the editing UI so
the list is visible while you draw.

![](../../docs/img/width-audit-panel.png)

### Install

Symlink the script into Glyphs's user-scripts folder (one-time):

```bash
ln -sf "$(pwd)/width_audit_panel.py" \
    "$HOME/Library/Application Support/Glyphs 3/Scripts/Width Audit Panel.py"
```

Then in Glyphs, hold **Option** and click the **Script** menu → **Reload
Scripts** (or just relaunch Glyphs). The panel now appears under
**Script → Width Audit Panel**.

### Picking a reference

Three sources, mixed into a single **Reference** dropdown at the top of the panel:

1. **Config** — every `[instances.*]` entry from `~/.glyph-audit/config.toml`. When you switch masters the dropdown auto-selects the matching entry, so the workflow is the same as the CLI's `--from-config`.
2. **System** — every font family the OS reports via `NSFontManager`. Pick e.g. *System · Helvetica* to compare against the macOS-installed copy.
3. **File** — *Choose file…* opens an Open dialog scoped to TTF/OTF/TTC; the picked file stays available as *File · …* for the rest of the session.

Right of the dropdown, **Edit config…** opens `~/.glyph-audit/config.toml` in your default editor (creating it from a starter template if it doesn't exist). Save in the editor, and the next refresh picks up the new entries — the dropdown rebuilds automatically.

```toml
[instances.Regular]
ref = "/path/to/Reference-Regular.ttf"

[instances.Bold]
ref = "/path/to/Reference-Bold.ttf"
```

Master names match case-insensitively. Variable fonts, system fonts, and Glyphs sources all work as `ref` values — same syntax as the CLI's `--pair` flag. Full schema → [../../docs/configuration.md](../../docs/configuration.md).

### Use

| Control | What it does |
|---|---|
| **Master menu** | Pick which master to audit. Defaults to the first. |
| **Filter menu** | `yellow` / `ready` / `all`. Same semantics as `glyph-audit --filter`. |
| **Live** | When ticked, the list refreshes on every Glyphs `UPDATEINTERFACE` notification — i.e. as you edit widths or change a glyph's colour. Untick to freeze the view while you investigate a row. |
| **Refresh** | Manual re-run. |
| **Reference menu** | Config entry / installed system font / picked file. See *Picking a reference* above. |
| **Edit config…** | Opens `~/.glyph-audit/config.toml` in the default editor; rebuilds the menu on return. |
| **List** | Glyph name, tier (T1 codepoint / T2 variant), target advance, reference advance, Δ, and the glyph's Glyphs colour label. Sorted by `|Δ|` descending. |
| Double-click a row | Switches the document to the row's master and opens the glyph in a new Glyphs tab. |

Running **Script → Width Audit Panel** again closes the open panel — the script is a toggle.

### Python dependencies

The script needs `GlyphAudit` importable in Glyphs's Python env. Two options:

1. **Symlink alongside a checkout** — if you cloned this repo, the script
   auto-adds the sibling `src/` directory to `sys.path` and you don't
   need to install anything. This is how the symlink above works when
   pointed at `examples/glyphs/width_audit_panel.py` inside the repo.

2. **Pip-install into Glyphs's Python** — for standalone deployments:

   ```bash
   /Applications/Glyphs\ 3.app/Contents/Frameworks/Python.framework/Versions/Current/bin/pip3 install glyph-audit
   ```

   (Path varies by Glyphs version and which Python module you've activated
   in Preferences → Addons → Modules. Glyphs's bundled Python is 3.11+.)

`vanilla` is bundled with Glyphs 3 — no install needed. TOML parsing uses
the stdlib `tomllib` (Python 3.11+), falling back to `tomli` if you're on
an older Glyphs Python.

### Troubleshooting

If clicking the menu item does nothing, open **Window → Macro Panel** —
exceptions from the script print there. The script also calls
`Glyphs.showMacroWindow()` automatically when it fails to launch.
