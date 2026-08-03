"""Loader for the `[instances.NAME]` section in the user config.

Each entry maps a target-master name to a reference spec plus an
optional axis location. Used by `--from-config` to build pairs without
the user typing them on the command line.

Example config:

    [instances.Regular]
    ref = "Reference-Regular.ttf"

    [instances.Bold]
    ref = "Inter[wght].ttf"
    axis = { wght = 700 }

    [instances.Light]
    ref = "Inter-Light-system"
    # axis = { wght = 300 }   # ignored for static system faces
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Optional


# Re-use the AI subpackage's defaults so there's one source of truth for
# the config file path.
from .defaults import DEFAULT_CONFIG_PATH, ConfigError


@dataclass
class InstanceSpec:
    name: str                            # target master name
    ref: str                             # raw spec passed to load_font
    axes: dict[str, float] = field(default_factory=dict)


def load_instances(config_path: Optional[str] = None) -> list[InstanceSpec]:
    """Return all `[instances.NAME]` entries from the config file. Returns
    an empty list if the file or section is absent (caller decides whether
    that's an error). Raises if `config_path` was passed explicitly but the
    file is unreadable or malformed."""
    explicit = config_path is not None
    path = config_path or DEFAULT_CONFIG_PATH

    if not os.path.isfile(path):
        if explicit:
            raise ConfigError(f"Config file not found: {path}")
        return []

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

    section = data.get("instances")
    if not section:
        return []

    out: list[InstanceSpec] = []
    for name, body in section.items():
        if not isinstance(body, dict):
            raise ConfigError(
                f"[instances.{name}] must be a TOML table; got {type(body).__name__}"
            )
        ref = body.get("ref")
        if not ref or not isinstance(ref, str):
            raise ConfigError(
                f"[instances.{name}] must define a string `ref` (file path, "
                f"system spec, or 'Name-system' form)."
            )
        axis = body.get("axis", {}) or {}
        if not isinstance(axis, dict):
            raise ConfigError(
                f"[instances.{name}].axis must be a TOML table mapping axis "
                f"tag -> number; got {type(axis).__name__}"
            )
        axes_floats: dict[str, float] = {}
        for tag, value in axis.items():
            try:
                axes_floats[str(tag)] = float(value)
            except (TypeError, ValueError) as e:
                raise ConfigError(
                    f"[instances.{name}].axis.{tag} must be numeric; got {value!r}"
                ) from e

        out.append(InstanceSpec(name=name, ref=ref, axes=axes_floats))

    return out
