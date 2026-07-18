#MenuTitle: Glyph Proof + Width Audit
# -*- coding: utf-8 -*-
"""Backwards-compat shim for the pre-split unified panel.

The unified panel used to live here. It's since been split into two
independent panels — `proof_panel.py` (Glyph Proof) and `audit_panel.py`
(Width Audit) — so each can be sized for its own use.

This file exists only so anyone with the old `Glyph Proof.py` symlink
still gets both panels when they run the menu item. `glyph-audit proof
panel install` replaces that symlink with two new ones pointing at the
new files; after re-running install, this shim is no longer reached.

Both entry points are wrapped in try/except so one panel failing to
open doesn't block the other from appearing.
"""

import sys
import traceback
from pathlib import Path

# sys.path bootstrap — inline because Glyphs.app runs this file as a
# standalone script when reached via a symlink, so relative imports have
# no parent package to resolve against. See audit_panel.py for the
# same pattern.
_HERE_INIT = Path(__file__).resolve()
if "GlyphAudit" not in sys.modules:
    for _depth in range(1, min(7, len(_HERE_INIT.parents))):
        _root = _HERE_INIT.parents[_depth]
        if (_root / "GlyphAudit" / "__init__.py").exists():
            if str(_root) not in sys.path:
                sys.path.insert(0, str(_root))
            break

# Purge cached GlyphAudit modules so stale code doesn't outlive
# "Reload Scripts" — see audit_panel.py for the rationale.
for _mod in [m for m in sys.modules if m == "GlyphAudit" or m.startswith("GlyphAudit.")]:
    del sys.modules[_mod]

from GlyphsApp import Glyphs


def _open_both():
    # Print a one-line hint so users know their symlink is stale — this
    # shows in the Macro Window the first time they hit the old menu
    # item post-split. Non-fatal; both panels still open below.
    print("Glyph Proof panel: opening both new panels (legacy symlink). "
          "Re-run `glyph-audit proof panel install` to update your Script menu.")

    try:
        from GlyphAudit.proof.panel.proof_panel import GlyphProofPanel
        GlyphProofPanel.toggle()
    except Exception:
        Glyphs.showMacroWindow()
        print("Glyph Proof: failed to open via shim.")
        print(traceback.format_exc())

    try:
        from GlyphAudit.proof.panel.audit_panel import WidthAuditPanel
        WidthAuditPanel.toggle()
    except Exception:
        Glyphs.showMacroWindow()
        print("Width Audit: failed to open via shim.")
        print(traceback.format_exc())


_open_both()
