"""Read/write helpers for the user-level audit config
(~/.glyph-audit/config.toml).

Deliberately vanilla/AppKit-free so it can be unit-tested outside
Glyphs.app — `common.py` (which needs vanilla for its dialogs) re-exports
these for the panels.
"""

from __future__ import annotations

import re
import subprocess
import traceback
from pathlib import Path
from typing import Optional

try:
    import tomllib  # 3.11+
except ImportError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        tomllib = None  # type: ignore


AUDIT_CONFIG_PATH = Path.home() / ".glyph-audit" / "config.toml"

CONFIG_TEMPLATE = """\
# GlyphAudit config — opened by the Width Audit panel's "Edit config…" button.
#
# Each [instances.NAME] entry maps a Glyphs master name (case-insensitive)
# to a reference font used by the Width Audit section.
#
# Examples:

# [instances.Regular]
# ref = "/Users/me/fonts/Reference-Regular.ttf"

# [instances.Bold]
# ref = "/Users/me/fonts/Reference-Bold.ttf"
"""


def load_audit_references(config_path: Optional[Path] = None) -> dict:
    """`[instances.*]` map, keys lowercased. Missing/malformed file → {}."""
    path = config_path or AUDIT_CONFIG_PATH
    if not path.exists() or tomllib is None:
        return {}
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return {}
    out = {}
    for name, entry in (data.get("instances") or {}).items():
        ref = entry.get("ref") if isinstance(entry, dict) else None
        if isinstance(ref, str):
            out[name.lower()] = ref
    return out


def pin_reference_for_master(
    master_name: str,
    ref_path: str,
    config_path: Optional[Path] = None,
) -> None:
    """Write/update `[instances.<master>] ref = "<path>"` in the audit
    config, preserving everything else in the file (comments included).

    Surgical edit, not a parse-and-rewrite: TOML round-trippers drop
    comments, and the bundled config template is mostly comments — users
    would lose their documentation on the first pin. Instead we locate
    the exact `[instances.<master>]` section by regex (case-insensitive,
    since readers lowercase the key) and either replace its `ref =` line
    or append a fresh section at the end of the file.
    """
    path = config_path or AUDIT_CONFIG_PATH
    master_key = master_name.strip()
    if not master_key or not ref_path:
        return
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(CONFIG_TEMPLATE, encoding="utf-8")

    text = path.read_text(encoding="utf-8")

    # TOML bare keys allow [A-Za-z0-9_-]; anything else ("Bold Italic")
    # must be quoted in the section header. Match BOTH forms when
    # searching — a user may have hand-quoted a bare-safe name.
    if re.match(r"^[A-Za-z0-9_-]+$", master_key):
        header = f"[instances.{master_key}]"
        header_re = re.compile(
            rf'^\[instances\.(?:"{re.escape(master_key)}"|{re.escape(master_key)})\][ \t]*$',
            re.MULTILINE | re.IGNORECASE,
        )
    else:
        header = f'[instances."{master_key}"]'
        header_re = re.compile(
            rf'^\[instances\."{re.escape(master_key)}"\][ \t]*$',
            re.MULTILINE | re.IGNORECASE,
        )

    escaped_path = ref_path.replace("\\", "\\\\").replace('"', '\\"')
    ref_line = f'ref = "{escaped_path}"'

    m = header_re.search(text)
    if m:
        # Replace the ref line inside this section (up to the next `[`
        # header or EOF). If the section somehow has no ref line, insert
        # one right after the header.
        section_start = m.end()
        next_header = re.search(r"^\[", text[section_start:], re.MULTILINE)
        section_end = section_start + next_header.start() if next_header else len(text)
        section = text[section_start:section_end]
        ref_re = re.compile(r"^[ \t]*ref[ \t]*=.*$", re.MULTILINE)
        if ref_re.search(section):
            new_section = ref_re.sub(lambda _: ref_line, section, count=1)
        else:
            new_section = "\n" + ref_line + section
        text = text[:section_start] + new_section + text[section_end:]
    else:
        if not text.endswith("\n"):
            text += "\n"
        text += f"\n{header}\n{ref_line}\n"

    path.write_text(text, encoding="utf-8")


def open_config_in_editor(config_path: Optional[Path] = None) -> None:
    path = config_path or AUDIT_CONFIG_PATH
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    try:
        # `open -t` forces the default TEXT editor. A bare `open` launches
        # whatever app claims .toml — on machines where Glyphs.app has
        # registered itself for text-like documents that's Glyphs itself,
        # which then tries to parse the config as a font source and
        # crashes the host app the panel is running in.
        subprocess.run(["open", "-t", str(path)], check=False)
    except Exception:
        traceback.print_exc()
