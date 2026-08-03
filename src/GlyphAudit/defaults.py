"""Loader for the `[defaults]` section in the user config.

Lets users skip frequently-used CLI flags by setting them once in
`~/.glyph-audit/config.toml`:

    [defaults]
    output    = "glyph-audit-report.md"
    filter    = "ready"
    tolerance = 1.0
    title     = "MyTypeface Coverage"
    no_normalize_upm = false
    from_config = true

CLI args still win when explicitly given. Anything not present in the
config falls through to argparse hard-coded defaults.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Optional


DEFAULT_CONFIG_PATH = os.path.expanduser("~/.glyph-audit/config.toml")


class ConfigError(Exception):
    """Raised when configuration cannot be resolved."""


# Recognised keys and their expected types. Keys not in this dict are
# ignored with a warning printed by the resolver.
DEFAULT_KEYS: dict[str, type] = {
    "output":           str,
    "filter":           str,
    "tolerance":        (int, float),
    "title":            str,
    "config":           str,    # rare but supported (could come from a wrapper)
    "no_normalize_upm": bool,
    "from_config":      bool,
}


def load_defaults(config_path: Optional[str] = None) -> dict[str, Any]:
    """Return the `[defaults]` section as a plain dict, or {} if absent."""
    explicit = config_path is not None
    path = config_path or DEFAULT_CONFIG_PATH

    if not os.path.isfile(path):
        if explicit:
            raise ConfigError(f"Config file not found: {path}")
        return {}

    try:
        if sys.version_info >= (3, 11):
            import tomllib
            with open(path, "rb") as f:
                data = tomllib.load(f)
        else:
            import tomli  # type: ignore
            with open(path, "rb") as f:
                data = tomli.load(f)
    except Exception as e:
        raise ConfigError(f"Failed to parse {path}: {e}") from e

    section = data.get("defaults") or {}
    if not isinstance(section, dict):
        raise ConfigError(f"[defaults] in {path} must be a TOML table.")

    cleaned: dict[str, Any] = {}
    for key, value in section.items():
        expected = DEFAULT_KEYS.get(key)
        if expected is None:
            # Unknown key — ignore quietly. The README documents what's
            # allowed, and an unknown key shouldn't block running.
            continue
        if not isinstance(value, expected):
            raise ConfigError(
                f"[defaults].{key} must be a {expected}; got {type(value).__name__}: "
                f"{value!r}"
            )
        # Expand ~ in path-like fields so users can write `~/...`.
        if key in ("output", "prompt", "config") and isinstance(value, str):
            value = os.path.expanduser(value)
        cleaned[key] = value

    return cleaned
