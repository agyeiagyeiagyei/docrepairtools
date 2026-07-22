"""Backwards-compat shim — the shared panel helpers were split along the
break-out boundary:

    audit_common.py  → audit-side helpers (state I/O, FontView bridge,
                       reference picker, TTFont cache) + configwrite
                       re-exports. No proof.config imports.
    proof_panel.py   → proof-only helpers (project-config discovery,
                       subprocess plumbing) now live with their sole
                       consumer.

Import from those modules directly; this file only keeps old imports
working and will be removed in a future release.
"""

from GlyphAudit.proof.panel.audit_common import *          # noqa: F401,F403
from GlyphAudit.proof.panel.audit_common import (          # noqa: F401
    AUDIT_CONFIG_PATH, AUDIT_STATE_PATH, CONFIG_TEMPLATE, MAX_RECENT_FILES,
)
from GlyphAudit.proof.panel.proof_panel import (           # noqa: F401
    DEV_SERVER_URL, PROOF_STATE_PATH,
    find_glyph_audit_cli, login_shell_path, project_config_for,
)
