"""
Layered safety controls for backrest-mcp.

All controls are opt-in to enable (default: most restrictive).
No env vars needed to stay safe — only set them to unlock writes.

Controls:
  BACKREST_READONLY (default: true)        — register only read-only tools
  BACKREST_ALLOW_DESTRUCTIVE (default: false) — also register forget/restore
  BACKREST_RESTORE_ALLOWED_PREFIX          — restore target path prefix
  BACKREST_AUDIT_LOG                       — path for write-op audit log (JSONL)
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import time

# Pattern for Backrest IDs (plan IDs, repo IDs, snapshot IDs, operation IDs).
# Allows alphanumeric, hyphens, underscores, and dots — rejects path traversal
# and shell metacharacters before IDs are forwarded to the Backrest API.
_ID_RE = re.compile(r'^[a-zA-Z0-9_.\-]{1,256}$')


def validate_backrest_id(name: str, value: str) -> None:
    """Validate a Backrest ID (plan, repo, snapshot, operation) against allowlist pattern.

    Raises ValueError if the value does not match ^[a-zA-Z0-9_.\\-]{1,256}$.
    """
    if not _ID_RE.match(value):
        raise ValueError(f"Invalid {name}: {value!r} — must match ^[a-zA-Z0-9_.-]{{1,256}}$")

# Master read-only switch. When true, no mutating tools are registered.
READONLY = os.getenv("BACKREST_READONLY", "true").lower() in ("1", "true", "yes")

# Separate gate for irreversible tools (forget_snapshot, restore_snapshot).
# Requires BACKREST_READONLY=false AND BACKREST_ALLOW_DESTRUCTIVE=true.
ALLOW_DESTRUCTIVE = (
    not READONLY
    and os.getenv("BACKREST_ALLOW_DESTRUCTIVE", "false").lower() in ("1", "true", "yes")
)

# Restore operations must write under this prefix. Prevents restoring over live data.
RESTORE_ALLOWED_PREFIX = os.getenv(
    "BACKREST_RESTORE_ALLOWED_PREFIX", "/tmp/backrest-restore/"
)

# Audit log path. Every non-read tool call is appended here as a JSON line.
AUDIT_LOG = os.getenv("BACKREST_AUDIT_LOG", "")


def audit_log(tool: str, args: dict) -> None:
    """Append a JSONL audit entry for a write/destructive tool call.

    Credentials are never included — the caller must not pass auth env values
    in the args dict.
    """
    if not AUDIT_LOG:
        return
    entry = {"ts": time.time(), "tool": tool, "args": args}
    pathlib.Path(AUDIT_LOG).parent.mkdir(parents=True, exist_ok=True)
    with open(AUDIT_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")
