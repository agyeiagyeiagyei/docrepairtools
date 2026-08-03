#MenuTitle: Coverage Check
# -*- coding: utf-8 -*-
"""Glyphs.app panel for two-way glyph-coverage comparison.

UI front-end for the same engine as `glyph-audit coverage` (see
GlyphAudit/coverage.py). Compares the ACTIVE font against a reference and
lists glyphs missing in EITHER direction:

  - Reference dropdown: "Auto (per-master config)" resolves the CURRENT
    master against its `[instances.*]` pin in ~/.glyph-audit/config.toml
    (the docrepair flow). Picking a specific config entry, a recent file,
    or "Choose file…" compares against that font instead — recents are
    shared with the other DocRepair panels.
  - The panel follows Glyphs' currently selected master, the same way the
    Width Audit panel does: switch masters in the font/edit view and the
    comparison re-runs against the new master.
  - Direction toggle: "Missing here" (reference has, your font lacks —
    the docrepair gate), "Missing in ref" (your font has, reference
    lacks), or "Both".
  - Every gap is listed in a searchable table (codepoint hex, glyph name,
    or Unicode name) with the reference glyph name and — when the glyph
    exists but is unencoded/unlinked — your glyph's name. Double-click a
    row to open that glyph for fixing; selecting a row explains in plain
    language what the status means and what the fix is.
  - The fix button retitles itself to what the selected row needs:
    "Assign Unicode" (unencoded glyph — codepoint appended to its
    unicode list, never replacing), "Create glyph" / "Create variant"
    (absent — an EMPTY glyph at the reference's advance width, scaled to
    your UPM; outlines are never copied, you draw them yourself;
    variants are named from YOUR base glyph + feature suffix, e.g.
    'zero.pnum', so the suffix convention wires them into the feature),
    "Add <feature> rule" (present-but-unlinked — appends
    'sub base by variant;' to the feature in Font Info; automatic
    features are refused). "Fix all" applies every "Missing here" fix
    for the current master in one undo group.
  - "Write report" saves the full markdown report (both directions,
    feature matching, full yes/no matrix) next to the source file.
  - "Emit .fea" decompiles the reference's GSUB into .fea files rewritten
    into target glyph names, saved next to the source.
  - Follows the active document: switch fonts and the panel re-checks
    against the new one.

Toggle: run the menu item again to close.

Install
-------
    pip install docrepair-tools
    glyph-audit proof panel install
"""

import sys
import traceback
from pathlib import Path

# sys.path bootstrap — inline because Glyphs.app runs a symlinked script
# as a standalone module (no `__package__`). Walk up until a directory
# with `GlyphAudit/__init__.py` and prepend it. No-op when importable.
_HERE_INIT = Path(__file__).resolve()
if "GlyphAudit" not in sys.modules:
    for _depth in range(1, min(7, len(_HERE_INIT.parents))):
        _root = _HERE_INIT.parents[_depth]
        if (_root / "GlyphAudit" / "__init__.py").exists():
            if str(_root) not in sys.path:
                sys.path.insert(0, str(_root))
            break

# Purge cached GlyphAudit modules so re-running the menu item picks up
# code changes without restarting Glyphs — see audit_panel.py.
for _mod in [m for m in sys.modules if m == "GlyphAudit" or m.startswith("GlyphAudit.")]:
    del sys.modules[_mod]

import vanilla
from GlyphsApp import Glyphs, GSFeature, GSGlyph, UPDATEINTERFACE

from GlyphAudit.coverage import (
    ABSENT,
    PRESENT_UNLINKED,
    UNENCODED_IN_TARGET,
    build_feature_file,
    coverage_gaps,
    feature_table,
    glyph_matrix,
    reverse_gaps,
    write_markdown,
)
from GlyphAudit.proof.panel.audit_common import (
    fontview_from_master,
    load_audit_references,
    load_recent_files,
    load_reference_cached,
    pick_font_file_async,
    push_recent_file,
)


# The open-panel registry lives on `builtins` — the module purge above
# gives each menu run a fresh class, so a class-level `_instance` would
# always be None and toggle-to-close would break. See audit_panel.py.
import builtins as _builtins


def _panel_registry() -> dict:
    reg = getattr(_builtins, "_glyphaudit_panel_registry", None)
    if reg is None:
        reg = {}
        _builtins._glyphaudit_panel_registry = reg
    return reg


# Direction toggle indices.
DIR_HERE = 0       # reference has it, your font lacks it
DIR_REF = 1        # your font has it, reference lacks it
DIR_BOTH = 2

_EXPLAIN_PROMPT = "Select a row above — what it means and how to fix it appears here."


class CoveragePanel:
    REGISTRY_KEY = "coverage"

    @classmethod
    def toggle(cls) -> None:
        reg = _panel_registry()
        existing = reg.get(cls.REGISTRY_KEY)
        if existing is not None:
            try:
                existing.close()
            except Exception:
                traceback.print_exc()
            reg[cls.REGISTRY_KEY] = None
            return
        if Glyphs.font is None:
            Glyphs.showMacroWindow()
            print("Coverage: open a font first.")
            return
        reg[cls.REGISTRY_KEY] = cls()

    def __init__(self) -> None:
        self.font = Glyphs.font
        self._live_subscribed = False
        # The single comparison currently shown: dict(master, target_view,
        # ref_view, fwd, rev). Follows Glyphs' selected master.
        self._current = None
        self._last_master_id = None
        self._option_specs = []
        self._user_picked_index = None
        self._summary_base = ""

        self.w = vanilla.FloatingWindow(
            (600, 480),
            "Coverage Check",
            autosaveName="GlyphAuditCoveragePanel",
            minSize=(520, 380),
        )

        # ----- row 1: reference picker + direction toggle -----
        self.w.refLabel = vanilla.TextBox((10, 14, 70, 20), "Reference", alignment="right")
        self.w.refMenu = vanilla.PopUpButton(
            (85, 12, -240, 22), [], callback=self._ref_picked_cb,
        )
        self.w.direction = vanilla.SegmentedButton(
            (-230, 12, 220, 22),
            [dict(title="Missing here"), dict(title="Missing in ref"), dict(title="Both")],
            callback=self._direction_cb,
        )
        self.w.direction.set(DIR_HERE)

        self.w.search = vanilla.SearchBox(
            (10, 44, -10, 22), callback=self._search_cb,
        )
        gap_cols = [
            dict(title="Glyph", key="glyph", width=50, editable=False),
            dict(title="Code", key="code", width=90, editable=False),
            dict(title="Ref glyph", key="ref", width=140, editable=False),
            dict(title="Your glyph", key="yours", width=140, editable=False),
            dict(title="Status", key="status", width=130, editable=False),
        ]
        self.w.gapList = vanilla.List(
            (10, 74, -10, -140), [],
            columnDescriptions=gap_cols,
            allowsMultipleSelection=False,
            selectionCallback=self._gap_selection_cb,
            doubleClickCallback=self._open_glyph_cb,
            autohidesScrollers=False,
        )

        # Plain-language explanation of the selected row.
        self.w.explain = vanilla.TextBox(
            (10, -132, -10, 38), _EXPLAIN_PROMPT, sizeStyle="small",
        )

        self.w.recheckBtn = vanilla.Button(
            (10, -88, 90, 24), "Re-check", callback=self._recheck_cb,
        )
        self.w.reportBtn = vanilla.Button(
            (108, -88, 100, 24), "Write report", callback=self._report_cb,
        )
        self.w.feaBtn = vanilla.Button(
            (216, -88, 86, 24), "Emit .fea", callback=self._fea_cb,
        )
        self.w.fixSelBtn = vanilla.Button(
            (310, -88, 130, 24), "Fix selected", callback=self._fix_selected_cb,
        )
        self.w.fixAllBtn = vanilla.Button(
            (-86, -88, 76, 24), "Fix all", callback=self._fix_all_cb,
        )
        self.w.summary = vanilla.TextBox((10, -56, -10, -10), "", sizeStyle="small")

        self.w.bind("close", self._on_close)
        self._rebuild_ref_menu(select_default=True)
        self._subscribe_live()
        self.w.open()
        self._run_checks()

    # ----- lifecycle ------------------------------------------------------

    def close(self) -> None:
        try:
            self._unsubscribe_live()
            self.w.close()
        finally:
            _panel_registry()[self.REGISTRY_KEY] = None

    def _on_close(self, sender) -> None:
        self._unsubscribe_live()
        _panel_registry()[self.REGISTRY_KEY] = None

    def _subscribe_live(self) -> None:
        if self._live_subscribed:
            return
        Glyphs.addCallback(self._on_update, UPDATEINTERFACE)
        self._live_subscribed = True

    def _unsubscribe_live(self) -> None:
        if not self._live_subscribed:
            return
        try:
            Glyphs.removeCallback(self._on_update)
        except Exception:
            pass
        self._live_subscribed = False

    def _on_update(self, info=None) -> None:
        # Follow the active document AND the selected master, like the
        # Width Audit panel: UPDATEINTERFACE fires on master switches, so
        # re-run only when the master actually changed — glyph edits still
        # need an explicit Re-check (a full two-way coverage pass per
        # keystroke is too heavy).
        try:
            if Glyphs.font is None:
                return
            if Glyphs.font is not self.font:
                self.font = Glyphs.font
                self._last_master_id = None
                self._run_checks()
                return
            master = self._current_master()
            mid = master.id if master is not None else None
            if mid != self._last_master_id:
                self._last_master_id = mid
                self._run_checks()
        except Exception:
            traceback.print_exc()

    # ----- reference picker -------------------------------------------------
    # Same grammar as the slant panel: "auto" (per-master config pin),
    # "config" (a specific [instances.*] entry), "file" (picked/recent),
    # "file_picker" (menu action).

    def _rebuild_ref_menu(self, *, select_default: bool) -> None:
        items = ["Auto (per-master config)"]
        specs = [("auto", None)]
        for master_lc, ref_path in load_audit_references().items():
            short = Path(ref_path).name if "/" in ref_path else ref_path
            items.append(f"Config · {master_lc} → {short}")
            specs.append(("config", master_lc))
        for path in load_recent_files():
            items.append(f"Recent · {Path(path).name}")
            specs.append(("file", path))
        items.append("Choose file…")
        specs.append(("file_picker", None))
        self._option_specs = specs
        self.w.refMenu.setItems(items)
        if select_default:
            self.w.refMenu.set(0)
            self._user_picked_index = None
        elif self._user_picked_index is not None and self._user_picked_index < len(specs):
            self.w.refMenu.set(self._user_picked_index)
        else:
            self.w.refMenu.set(0)

    def _ref_picked_cb(self, sender) -> None:
        idx = sender.get()
        if idx < 0 or idx >= len(self._option_specs):
            return
        kind, ident = self._option_specs[idx]
        if kind == "file_picker":
            if self._user_picked_index is not None and self._user_picked_index < len(self._option_specs):
                self.w.refMenu.set(self._user_picked_index)
            else:
                self.w.refMenu.set(0)
            try:
                parent = self.w.getNSWindow()
            except Exception:
                parent = None
            pick_font_file_async(parent, on_picked=self._apply_picked_file)
            return
        self._user_picked_index = idx
        self._run_checks()

    def _apply_picked_file(self, path: str) -> None:
        push_recent_file(path)
        self._rebuild_ref_menu(select_default=False)
        for i, (k, ident) in enumerate(self._option_specs):
            if k == "file" and ident == path:
                self.w.refMenu.set(i)
                self._user_picked_index = i
                break
        self._run_checks()

    def _picked_spec(self):
        """(kind, ident) of the current dropdown selection."""
        idx = self.w.refMenu.get()
        if 0 <= idx < len(self._option_specs):
            return self._option_specs[idx]
        return ("auto", None)

    # ----- checks -----------------------------------------------------------

    def _current_master(self):
        master = self.font.selectedFontMaster
        if master is None and self.font.masters:
            master = self.font.masters[0]
        return master

    def _pair_to_check(self):
        """(master, ref_spec) for the CURRENT master, or (master, None)
        when the dropdown's auto mode has no pin for it."""
        kind, ident = self._picked_spec()
        master = self._current_master()
        if master is None:
            return None, None
        refs = load_audit_references()
        if kind == "auto":
            spec = refs.get(master.name.lower())
        elif kind == "config":
            spec = refs.get(ident)
        else:  # file
            spec = ident
        return master, spec

    @staticmethod
    def _counts(result):
        absent = sum(1 for g in result.codepoint_gaps if g.kind == ABSENT)
        absent += sum(1 for g in result.variant_gaps if g.kind == ABSENT)
        warn = len(result.codepoint_gaps) + len(result.variant_gaps) - absent
        return absent, warn

    def _run_checks(self) -> None:
        self._current = None
        master, spec = self._pair_to_check()
        if master is not None:
            self._last_master_id = master.id
        if master is None:
            self._summary_base = "No master selected."
            self.w.summary.set(self._summary_base)
            self.w.gapList.set([])
            return
        if not spec:
            kind, _ident = self._picked_spec()
            if kind == "auto":
                self._summary_base = (
                    f"{master.name}: no reference pinned — pick one above, or pin "
                    f"[instances.{master.name.lower()}] in ~/.glyph-audit/config.toml."
                )
            else:
                self._summary_base = "Nothing to check — pick a reference file."
            self.w.summary.set(self._summary_base)
            self.w.gapList.set([])
            return

        try:
            ref_view = load_reference_cached(spec)
        except Exception as e:
            self._summary_base = f"{master.name}: reference failed to load ({e})"
            self.w.summary.set(self._summary_base)
            self.w.gapList.set([])
            return

        target_view = fontview_from_master(self.font, master)
        fwd = coverage_gaps(target_view, ref_view, pair_label=master.name)
        fwd.reverse = reverse_gaps(target_view, ref_view, pair_label=master.name)
        fwd.feature_rows = feature_table(target_view, ref_view)
        fwd.cp_matrix, fwd.var_matrix = glyph_matrix(target_view, ref_view)
        self._current = dict(
            master=master, target_view=target_view, ref_view=ref_view,
            fwd=fwd, rev=fwd.reverse,
        )

        here_a, here_w = self._counts(fwd)
        there_a, there_w = self._counts(fwd.reverse)
        feat_missing = sum(1 for fr in fwd.feature_rows
                           if fr.status == "missing" and fr.ref_rules)
        feat_partial = sum(1 for fr in fwd.feature_rows
                           if fr.status == "partial")
        status = "PASS" if fwd.absent_count() == 0 else "FAIL"
        self._summary_base = (
            f"{master.name}: {here_a} absent · {here_w} warn here; "
            f"{there_a} · {there_w} warn in ref; features {feat_missing} missing · "
            f"{feat_partial} partial — {status} (follows 'Missing here')."
        )
        self._rebuild_gap_list()

    # ----- gap list -----------------------------------------------------------

    def _direction_cb(self, sender) -> None:
        self._rebuild_gap_list()

    def _search_cb(self, sender) -> None:
        self._rebuild_gap_list()

    def _rows_from_result(self, result, dir_label):
        out = []
        for g in result.codepoint_gaps:
            yours = g.ref_glyph if g.kind == UNENCODED_IN_TARGET else "—"
            out.append(dict(
                glyph=g.char,
                code=f"U+{g.codepoint:04X}",
                ref=g.ref_glyph,
                yours=yours,
                status=f"{dir_label} · {'absent' if g.kind == ABSENT else 'unencoded'}",
                _open=yours if g.kind == UNENCODED_IN_TARGET else None,
                _kind=g.kind,
                _dir=dir_label,
                _cp=g.codepoint,
                _feature=None,
                _uname=g.unicode_name,
                _hay=f"u+{g.codepoint:04x} {g.char} {g.ref_glyph} {g.unicode_name}".lower(),
            ))
        for g in result.variant_gaps:
            yours = g.ref_glyph if g.kind == PRESENT_UNLINKED else "—"
            out.append(dict(
                glyph=g.base_char,
                code=f"U+{g.base_codepoint:04X} · {g.feature}",
                ref=g.ref_glyph,
                yours=yours,
                status=f"{dir_label} · {'absent' if g.kind == ABSENT else 'unlinked'}",
                _open=yours if g.kind == PRESENT_UNLINKED else None,
                _kind=g.kind,
                _dir=dir_label,
                _cp=g.base_codepoint,
                _feature=g.feature,
                _uname="",
                _hay=f"u+{g.base_codepoint:04x} {g.base_char} {g.feature} {g.ref_glyph}".lower(),
            ))
        return out

    @staticmethod
    def _row_key(r):
        """Identity of a gap row across rebuilds (direction + target)."""
        return (r["_dir"], r["code"], r["ref"], r["_feature"])

    def _rebuild_gap_list(self) -> None:
        if self._current is None:
            self.w.gapList.set([])
            self.w.summary.set(self._summary_base)
            return
        # Preserve the selection across the rebuild — re-checks, master
        # switches and font-focus changes all regenerate the list, and
        # losing the selection mid-workflow reads as the panel ignoring
        # clicks ("Select a gap row first" right after selecting one).
        prev_sel = self.w.gapList.getSelection()
        prev_key = None
        if prev_sel:
            try:
                prev_key = self._row_key(self.w.gapList.get()[prev_sel[0]])
            except Exception:
                prev_key = None
        direction = self.w.direction.get()
        rows = []
        if direction in (DIR_HERE, DIR_BOTH):
            rows += self._rows_from_result(self._current["fwd"], "here")
        if direction in (DIR_REF, DIR_BOTH):
            rows += self._rows_from_result(self._current["rev"], "in ref")
        query = self.w.search.get().strip().lower()
        if query:
            rows = [r for r in rows if query in r["_hay"]]
        self.w.gapList.set(rows)
        restored = None
        if prev_key is not None:
            for i, r in enumerate(rows):
                if self._row_key(r) == prev_key:
                    restored = i
                    break
        if restored is not None:
            self.w.gapList.setSelection([restored])
            # setSelection doesn't fire selectionCallback — refresh the
            # explainer + fix-button title manually.
            self._gap_selection_cb(self.w.gapList)
        else:
            self._update_fix_button(None)
        summary = self._summary_base
        missing_feats = [fr.feature for fr in self._current["fwd"].feature_rows
                         if fr.status == "missing" and fr.ref_rules]
        if missing_feats:
            summary += (
                f"\n{self._current['master'].name}: features missing — "
                + ", ".join(missing_feats)
            )
        self.w.summary.set(summary)

    def _open_glyph_cb(self, sender) -> None:
        sel = sender.getSelection()
        if not sel:
            return
        name = sender.get()[sel[0]].get("_open")
        if name and self.font.glyphs[name]:
            try:
                self.font.newTab("/" + name)
            except Exception:
                traceback.print_exc()

    # ----- plain-language explanation ----------------------------------------

    def _base_name_for(self, cp):
        """Glyph name for a codepoint, from the target first, else reference."""
        if self._current is None or cp is None:
            return None
        return (self._current["target_view"].cmap.get(cp)
                or self._current["ref_view"].cmap.get(cp))

    def _gap_selection_cb(self, sender) -> None:
        sel = sender.getSelection()
        if not sel:
            self.w.explain.set(_EXPLAIN_PROMPT)
            self._update_fix_button(None)
            return
        r = sender.get()[sel[0]]
        self._update_fix_button(r)
        kind = r["_kind"]
        feature = r["_feature"]

        if r["_dir"] == "in ref":
            self.w.explain.set(
                f"'{r['ref']}' is covered by your font but not by the reference. "
                "Informational only — it never affects PASS/FAIL and there is "
                "nothing to fix. Remove it only if it crept in by accident."
            )
            return

        if kind == UNENCODED_IN_TARGET:
            self.w.explain.set(
                f"You already have '{r['yours']}' — it just has no Unicode value, "
                f"so {r['code']} renders with a fallback font in documents. "
                f"Fix: 'Assign Unicode' sets U+{r['_cp']:04X} on it (added to any "
                "existing values, never replacing) — no redraw needed."
            )
            return

        if kind == PRESENT_UNLINKED:
            base = self._base_name_for(r["_cp"])
            rule = f"sub {base} by {r['yours']};" if base else f"sub <base> by {r['yours']};"
            self.w.explain.set(
                f"You have '{r['yours']}', but no {feature} rule points "
                f"'{r['glyph']}' at it, so documents never reach it. Fix: "
                f"'Add {feature} rule' appends '{rule}' to your {feature} "
                "feature in Font Info (created if missing; automatic features "
                "are refused — Glyphs would regenerate them)."
            )
            return

        # ABSENT
        if feature:
            base = self._base_name_for(r["_cp"])
            vname = f"{base}.{feature}" if base else r["ref"]
            self.w.explain.set(
                f"The reference substitutes '{r['glyph']}' → '{r['ref']}' via the "
                f"{feature} feature; your font has no such variant glyph. Fix: "
                f"'Create variant' makes an empty '{vname}' at the reference's "
                "advance width, ready to draw — the .{feature} suffix wires it "
                "into the feature automatically."
            )
        else:
            self.w.explain.set(
                f"The reference covers {r['code']} ({r['_uname'].lower() or 'unencoded'}) "
                f"as '{r['ref']}'; your font has no glyph for it at all, so it "
                "falls back to another font in documents. Fix: 'Create glyph' "
                "makes an empty glyph at the reference's advance width with "
                "the Unicode assigned — you draw the outline."
            )

    def _update_fix_button(self, r) -> None:
        """Retitle the fix button to the action the SELECTED row needs."""
        title = "Fix selected"
        if r is not None and r["_dir"] == "here":
            if r["_kind"] == UNENCODED_IN_TARGET:
                title = "Assign Unicode"
            elif r["_kind"] == PRESENT_UNLINKED:
                title = f"Add {r['_feature']} rule"
            elif r["_feature"]:
                title = "Create variant"
            else:
                title = "Create glyph"
        self.w.fixSelBtn.setTitle(title)

    # ----- gap fixes ----------------------------------------------------------

    def _ref_advance(self, ref_view, ref_name):
        """Reference advance for ref_name, scaled into our UPM (or None)."""
        adv = ref_view.advances.get(ref_name)
        if adv is None:
            return None
        ref_upm = ref_view.upm or self.font.upm
        return int(round(adv * self.font.upm / float(ref_upm)))

    @staticmethod
    def _nice_name(ref_name):
        """Glyphs' display name for a reference production name, when known."""
        try:
            nice = Glyphs.niceGlyphName(ref_name)
            if nice:
                return nice
        except Exception:
            pass
        return ref_name

    @staticmethod
    def _assign_unicode(glyph, cp):
        """Append cp to the glyph's unicode list — never replace. The
        singular `glyph.unicode =` setter WIPES existing values (space
        lost U+0020 when given U+00A0, mirroring the reference where one
        glyph legitimately covers both). Returns None on success, else a
        why-string. Read back after writing: a value that doesn't stick
        is a failure, not an assumption."""
        existing = list(glyph.unicodes or [])
        try:
            if cp in {int(u, 16) for u in existing}:
                return None  # already encoded
        except Exception:
            pass
        glyph.unicodes = existing + [f"{cp:04X}"]
        try:
            stuck = any(int(u, 16) == cp for u in (glyph.unicodes or []))
        except Exception:
            stuck = True  # can't verify — don't cry wolf
        if not stuck:
            return f"U+{cp:04X} did not stick (read-back mismatch)"
        return None

    def _add_feature_rule(self, tag, base, variant):
        """Append 'sub base by variant;' to feature `tag` in Font Info,
        creating the feature when missing. A rule already present —
        including inside Glyphs-GENERATED automatic code — counts as
        wired. Otherwise automatic features are refused: Glyphs
        regenerates those and would drop the manual line. Returns None
        on success, else a why-string."""
        rule = f"sub {base} by {variant};"
        existing = None
        try:
            for f in self.font.features:
                if getattr(f, "name", None) == tag:
                    existing = f
                    break
        except Exception:
            existing = None
        if existing is not None:
            code = existing.code or ""
            if rule in code:
                return None  # already wired (manual or auto-generated)
            if getattr(existing, "automatic", False):
                return (f"{tag} is automatic and does NOT generate this rule — "
                        f"a manual addition would be lost on regeneration; "
                        f"name the glyph with a .{tag} suffix instead, or make "
                        f"the feature manual in Font Info first")
            existing.code = code.rstrip() + ("\n\n" if code.strip() else "") + rule + "\n"
            return None
        try:
            feat = GSFeature()
            feat.name = tag
            feat.code = rule + "\n"
            self.font.features.append(feat)
        except Exception as e:
            return f"couldn't create {tag} feature: {e}"
        return None

    def _apply_fix(self, kind, cp, feature, ref_name):
        """Fix one 'Missing here' gap. Returns (token, info):

        token "created"  — empty glyph made at the reference's advance
                           width [+unicode]; info has name/adv/cp/existed.
                           Outlines are never copied — the user draws them.
                           Variant glyphs are named from the TARGET's base
                           glyph + feature suffix ('zero.pnum', not the
                           reference's production name) so the suffix map
                           wires them into the feature immediately.
        token "encoded"  — unicode assigned to an existing glyph;
                           info has name/cp.
        token "linked"   — 'sub base by variant;' added to the feature;
                           info has name/feature/base.
        token "failed"   — info["why"] says what went wrong.
        """
        if self._current is None:
            return "failed", {"why": "no comparison loaded"}
        if kind == UNENCODED_IN_TARGET:
            glyph = self.font.glyphs[ref_name]
            if glyph is None:
                return "failed", {"name": ref_name, "why": "glyph not found in font"}
            why = self._assign_unicode(glyph, cp)
            if why:
                return "failed", {"name": ref_name, "why": why}
            return "encoded", {"name": glyph.name, "cp": cp}
        if kind == PRESENT_UNLINKED:
            base = self._current["target_view"].cmap.get(cp)
            if base is None:
                return "failed", {"name": ref_name,
                                  "why": f"your font has no base glyph for U+{cp:04X}"}
            why = self._add_feature_rule(feature, base, ref_name)
            if why:
                return "failed", {"name": ref_name, "why": why}
            return "linked", {"name": ref_name, "feature": feature, "base": base}
        # ABSENT — create the glyph at the reference's advance width.
        master = self._current["master"]
        adv = self._ref_advance(self._current["ref_view"], ref_name)
        name = self._nice_name(ref_name)
        if feature is not None:
            base = self._current["target_view"].cmap.get(cp)
            if base:
                name = f"{base}.{feature}"
        glyph = self.font.glyphs[name] or self.font.glyphs[ref_name]
        existed = glyph is not None
        if not existed:
            glyph = GSGlyph(name)
            self.font.glyphs.append(glyph)
        if cp is not None and feature is None:
            why = self._assign_unicode(glyph, cp)
            if why:
                return "failed", {"name": glyph.name, "why": why}
        layer = glyph.layers[master.id]
        if layer is None:
            return "failed", {"name": glyph.name,
                              "why": f"no layer for master {master.name}"}
        if adv is not None:
            layer.width = adv
        return "created", {"name": glyph.name, "cp": cp, "adv": adv,
                           "existed": existed, "feature": feature}

    def _fix_selected_cb(self, sender) -> None:
        try:
            if self._current is None:
                self.w.summary.set("Nothing checked yet.")
                return
            sel = self.w.gapList.getSelection()
            if not sel:
                self.w.summary.set("Select a gap row first.")
                return
            r = self.w.gapList.get()[sel[0]]
            if r["_dir"] != "here":
                self.w.summary.set(
                    "'in ref' rows are extras in YOUR font — nothing to fix "
                    "from the reference."
                )
                return
            master_name = self._current["master"].name
            token, info = self._apply_fix(r["_kind"], r["_cp"], r["_feature"], r["ref"])
            if token in ("created", "encoded", "linked"):
                # Re-check FIRST — it overwrites the summary line — then
                # report. Open the glyph so the result is visible.
                self._run_checks()
                name = info.get("name")
                if name and self.font.glyphs[name]:
                    try:
                        self.font.newTab("/" + name)
                    except Exception:
                        pass
            if token == "created":
                verb = "Updated existing" if info["existed"] else "Created"
                msg = f"{verb} '{info['name']}' in {master_name}"
                if info["adv"] is not None:
                    msg += f" at {info['adv']} units"
                if info["cp"] is not None and not info.get("feature"):
                    msg += f" with U+{info['cp']:04X}"
                msg += " — opened in a tab, ready to draw."
                if info.get("feature"):
                    msg += f" The .{info['feature']} suffix wires it into the feature."
            elif token == "encoded":
                msg = f"Assigned U+{info['cp']:04X} to '{info['name']}' — row should be gone from the list."
            elif token == "linked":
                msg = (f"Added 'sub {info['base']} by {info['name']};' to your "
                       f"{info['feature']} feature — row should be gone from the list.")
            else:
                msg = f"Couldn't fix '{r['ref']}': {info.get('why', 'unknown')} (see Macro window)."
            print(f"Coverage fix: {msg}")
            self.w.summary.set(msg)
        except Exception:
            Glyphs.showMacroWindow()
            print("Coverage: fix failed.")
            print(traceback.format_exc())

    def _fix_all_cb(self, sender) -> None:
        """Fix every 'Missing here' gap for the current master — absent
        glyphs are created empty at the reference's advance width,
        unencoded glyphs get their Unicode value, unlinked variants get
        their feature rule appended (automatic features refused)."""
        try:
            if self._current is None:
                self.w.summary.set("Nothing checked yet.")
                return
            fwd = self._current["fwd"]
            counts = {"created": 0, "encoded": 0, "linked": 0, "failed": 0}
            failures = []
            try:
                self.font.undoManager().beginUndoGrouping()
            except Exception:
                pass
            try:
                for g in fwd.codepoint_gaps:
                    token, info = self._apply_fix(g.kind, g.codepoint, None, g.ref_glyph)
                    counts[token] += 1
                    if token == "failed":
                        failures.append(f"{g.ref_glyph}: {info.get('why', '?')}")
                for g in fwd.variant_gaps:
                    token, info = self._apply_fix(g.kind, g.base_codepoint, g.feature, g.ref_glyph)
                    counts[token] += 1
                    if token == "failed":
                        failures.append(f"{g.ref_glyph}: {info.get('why', '?')}")
            finally:
                try:
                    self.font.undoManager().endUndoGrouping()
                except Exception:
                    pass
            parts = []
            if counts["created"]:
                parts.append(f"{counts['created']} glyph(s) created at reference width")
            if counts["encoded"]:
                parts.append(f"{counts['encoded']} Unicode value(s) assigned")
            if counts["linked"]:
                parts.append(f"{counts['linked']} feature rule(s) added")
            if counts["failed"]:
                parts.append(f"{counts['failed']} failed — see Macro window")
            msg = "Fix all: " + (", ".join(parts) if parts else "nothing to fix.")
            print(f"Coverage {msg}")
            for f in failures:
                print(f"  failed — {f}")
            # Re-check FIRST (it overwrites the summary), then report.
            if counts["created"] or counts["encoded"] or counts["linked"]:
                self._run_checks()
            self.w.summary.set(msg)
        except Exception:
            Glyphs.showMacroWindow()
            print("Coverage: fix-all failed.")
            print(traceback.format_exc())

    # ----- callbacks ---------------------------------------------------------

    def _recheck_cb(self, sender) -> None:
        try:
            if Glyphs.font is not None and Glyphs.font is not self.font:
                self.font = Glyphs.font
            self._run_checks()
        except Exception:
            Glyphs.showMacroWindow()
            print("Coverage: check failed.")
            print(traceback.format_exc())

    def _output_dir(self) -> Path:
        """Directory next to the source file (falls back to home)."""
        if self.font.filepath:
            return Path(self.font.filepath).parent
        return Path.home()

    def _report_cb(self, sender) -> None:
        try:
            if self._current is None:
                self.w.summary.set("Nothing checked yet.")
                return
            out = self._output_dir() / "coverage-report.md"
            write_markdown([self._current["fwd"]], str(out))
            self.w.summary.set(f"Wrote {out}")
        except Exception:
            Glyphs.showMacroWindow()
            print("Coverage: report failed.")
            print(traceback.format_exc())

    def _fea_cb(self, sender) -> None:
        try:
            if self._current is None:
                self.w.summary.set("Nothing checked yet.")
                return
            out_dir = self._output_dir() / "coverage-features"
            out_dir.mkdir(parents=True, exist_ok=True)
            row = self._current
            fea_text, stats = build_feature_file(row["target_view"], row["ref_view"])
            safe = "".join(c if c.isalnum() or c in "-_" else "_"
                           for c in row["master"].name)
            path = out_dir / f"{safe}.fea"
            path.write_text(fea_text, encoding="utf-8")
            self.w.summary.set(
                f"Wrote {path} — {row['master'].name}: {stats['features']} features, "
                f"{stats['rules']} rules ({stats['skipped']} skipped)"
            )
        except Exception:
            Glyphs.showMacroWindow()
            print("Coverage: .fea emit failed.")
            print(traceback.format_exc())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

try:
    CoveragePanel.toggle()
except Exception:
    Glyphs.showMacroWindow()
    print("Coverage: failed to launch.")
    print(traceback.format_exc())
