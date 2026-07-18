"""Proof pipeline: subset a Glyphs source down to a proof TTF and serve a
web app that compares it against reference fonts.

Public surface:
    from GlyphAudit.proof.config import load_project_config, ProofConfig
    from GlyphAudit.proof.build  import build_proof_font, watch_and_rebuild
"""

from .config import (
    ProofConfig,
    ProjectConfig,
    Reference,
    ReferenceSlot,
    load_project_config,
    ConfigError,
    GLYPHS_COLORS,
    DEFAULT_PROOF_COLORS,
    normalize_color,
    validate_colors,
)
from .build import (
    build_font as build_proof_font,   # exported under the public name
    watch_and_rebuild,
    output_paths_for,
    write_proof_config,
    write_width_manifests,
)

__all__ = [
    "ProofConfig",
    "ProjectConfig",
    "Reference",
    "ReferenceSlot",
    "load_project_config",
    "ConfigError",
    "GLYPHS_COLORS",
    "DEFAULT_PROOF_COLORS",
    "normalize_color",
    "validate_colors",
    "build_proof_font",
    "watch_and_rebuild",
    "output_paths_for",
    "write_proof_config",
    "write_width_manifests",
]
