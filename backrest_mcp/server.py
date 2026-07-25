"""
backrest-mcp — FastMCP server wrapping the Backrest backup manager REST API.

Reconciled against the deployed Backrest v1.13.0 connect-rpc API.

Tool registration is gated by safety flags from backrest_mcp.safety:
  Always registered (read-only):
    get_health, get_config, list_snapshots, list_snapshot_files, get_summary,
    get_operations, get_logs, get_download_url

  Registered when BACKREST_READONLY=false:
    trigger_backup, do_repo_task, cancel_operation

  Registered when BACKREST_READONLY=false AND BACKREST_ALLOW_DESTRUCTIVE=true:
    forget_snapshot, restore_snapshot
"""

from __future__ import annotations

import datetime
import os
import pathlib
import time
from typing import Literal, Optional

import httpx
import structlog
from fastmcp import FastMCP
from fastmcp.server.auth import StaticTokenVerifier

from .client import BackrestClient, get_client
from .models import (
    ListSnapshotFilesResponse,
    OperationList,
    SnapshotList,
    SummaryDashboard,
)
from .observability import emit_metric
from .safety import (
    ALLOW_DESTRUCTIVE,
    READONLY,
    RESTORE_ALLOWED_PREFIX,
    audit_log,
    validate_backrest_id,
)

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Transport configuration (stdio default; http for the long-lived PM2 service)
# ---------------------------------------------------------------------------
# Hosts treated as loopback for the non-loopback fail-closed guard in main().
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
# The auth token must be long enough that a log-scrubbing filter never treats it as
# too-short-to-redact. secrets.token_hex(32) yields 64 chars, well above this floor.
_MIN_AUTH_TOKEN_LENGTH = 16

TRANSPORT = os.getenv("BACKREST_MCP_TRANSPORT", "stdio").lower()
HTTP_HOST = os.getenv("BACKREST_MCP_HTTP_HOST", "127.0.0.1")
HTTP_PORT = int(os.getenv("BACKREST_MCP_HTTP_PORT", "8626"))
AUTH_TOKEN = os.getenv("BACKREST_MCP_AUTH_TOKEN", "")
ALLOW_NONLOOPBACK = os.getenv("BACKREST_MCP_ALLOW_NONLOOPBACK", "").lower() in ("1", "true", "yes")

# Auth is gated on BACKREST_MCP_AUTH_TOKEN being set, independent of transport — stdio
# has no HTTP surface so it only matters when TRANSPORT=http (enforced in main()).
_auth = None
if AUTH_TOKEN:
    _auth = StaticTokenVerifier(tokens={AUTH_TOKEN: {"sub": "scoped-mcp", "client_id": "cli"}})

mcp = FastMCP(
    name="backrest-mcp",
    instructions=(
        "Backrest MCP server — read/manage the Backrest backup manager running on forge "
        "(http://localhost:9898). Provides read access to backup operations, snapshots, "
        "configuration, dashboard stats, and operation logs. Start with get_health to "
        "confirm the instance is reachable and credentials are valid. "
        "Write tools (trigger_backup, do_repo_task, cancel_operation) are only registered "
        "when BACKREST_READONLY=false. Destructive tools (forget_snapshot, restore_snapshot) "
        "additionally require BACKREST_ALLOW_DESTRUCTIVE=true. RunCommand/SetConfig/AddRepo/"
        "RemoveRepo/ClearHistory are intentionally not exposed."
    ),
    auth=_auth,
)

_REPO_TASK_MAP: dict[str, int] = {
    "index": 1,
    "prune": 2,
    "check": 3,
    "stats": 4,
    "unlock": 5,
    "forget": 6,
}

_STATUS_ICONS: dict[str, str] = {
    "STATUS_SUCCESS": "✓",
    "STATUS_ERROR": "✗",
    "STATUS_INPROGRESS": "⟳",
    "STATUS_PENDING": "○",
    "STATUS_WARNING": "⚠",
    "STATUS_CANCELLED": "⊘",
}


def _tool_error(tool: str, err: Exception) -> dict:
    log.error("tool_error", tool=tool, error=str(err))
    return {"error": "tool call failed — check server logs for details"}


def _fmt_ms(ms: Optional[int]) -> str:
    if ms is None:
        return "—"
    return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _fmt_duration(start_ms: Optional[int], end_ms: Optional[int]) -> str:
    if start_ms is None or end_ms is None:
        return ""
    secs = (end_ms - start_ms) / 1000
    if secs < 60:
        return f"{secs:.0f}s"
    return f"{secs / 60:.1f}m"


async def _list_repos(client: BackrestClient) -> list[dict]:
    """Return the list of configured repos ({id, guid, ...}) from GetConfig."""
    cfg = await client.post("GetConfig", {})
    return cfg.get("repos", []) or []


async def _resolve_repo_guid(client: BackrestClient, repo_id: str) -> str:
    """Resolve a human repo ID to its GUID.

    Several Backrest APIs (ListSnapshotFiles, OpSelector) key on the repo GUID rather
    than the human ID, so tools accept the ID for ergonomics and resolve it here.
    """
    for r in await _list_repos(client):
        if r.get("id") == repo_id:
            guid = r.get("guid")
            if guid:
                return guid
    raise ValueError(f"repo not found: {repo_id!r}")


# ---------------------------------------------------------------------------
# Read-only tools — always registered
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_health() -> dict:
    """Check that Backrest is reachable and credentials are valid.

    Wraps GetConfig — the call that returns 200 with valid credentials and 401 when they
    drift. Returns status "ok", "auth_failed", or "unreachable", plus the configured URL
    and a repo/plan count when healthy. Read-only; safe to poll.
    """
    client = get_client()
    backrest_url = os.environ.get("BACKREST_URL", "http://localhost:9898")
    t0 = time.perf_counter()
    try:
        cfg = await client.post("GetConfig", {})
        await emit_metric("backrest_tool", {"tool": "get_health", "status": "ok"}, {"duration_ms": (time.perf_counter() - t0) * 1000})
        return {
            "status": "ok",
            "backrest_url": backrest_url,
            "repos": len(cfg.get("repos", []) or []),
            "plans": len(cfg.get("plans", []) or []),
            "config_version": cfg.get("version"),
        }
    except httpx.HTTPStatusError as e:
        status = "auth_failed" if e.response.status_code in (401, 403) else "unreachable"
        log.error("get_health", status=status, http_status=e.response.status_code)
        return {"status": status, "backrest_url": backrest_url, "http_status": e.response.status_code}
    except Exception as e:
        log.error("get_health", status="unreachable", error=str(e))
        return {"status": "unreachable", "backrest_url": backrest_url}


@mcp.tool()
async def get_config() -> dict:
    """Read the Backrest configuration (repos, plans, global settings).

    Returns the full Backrest config as a dict. Does not expose credentials.
    """
    client = get_client()
    t0 = time.perf_counter()
    try:
        result = await client.post("GetConfig", {})
        await emit_metric("backrest_tool", {"tool": "get_config"}, {"duration_ms": (time.perf_counter() - t0) * 1000})
        return result
    except Exception as e:
        return _tool_error("get_config", e)


@mcp.tool()
async def list_snapshots(
    repo_id: Optional[str] = None,
    plan_id: Optional[str] = None,
) -> dict:
    """List snapshots in a Backrest repo, optionally filtered by repo or plan.

    Args:
        repo_id: Backrest repository ID to list snapshots for. When omitted, all
                 configured repos are enumerated and their snapshots merged (each
                 snapshot is tagged with its repoId).
        plan_id: Plan ID to filter snapshots. Can be combined with repo_id.

    Returns a list of snapshots with ID, timestamp, hostname, paths, tags, and repoId.
    """
    client = get_client()
    try:
        if repo_id:
            validate_backrest_id("repo_id", repo_id)
        if plan_id:
            validate_backrest_id("plan_id", plan_id)
    except ValueError as e:
        return _tool_error("list_snapshots", e)

    t0 = time.perf_counter()
    try:
        # ListSnapshots rejects an empty repo_id (HTTP 500), so when none is given we
        # enumerate configured repos and merge results.
        if repo_id:
            repo_ids = [repo_id]
        else:
            repo_ids = [r["id"] for r in await _list_repos(client) if r.get("id")]

        merged: list = []
        for rid in repo_ids:
            body: dict = {"repoId": rid}
            if plan_id:
                body["planId"] = plan_id
            raw = await client.post("ListSnapshots", body)
            parsed = SnapshotList.model_validate(raw)
            for s in parsed.snapshots:
                s.repoId = rid
                merged.append(s)

        await emit_metric("backrest_tool", {"tool": "list_snapshots"}, {"duration_ms": (time.perf_counter() - t0) * 1000, "count": len(merged)})
        return {"snapshots": [s.model_dump(exclude_none=True) for s in merged]}
    except Exception as e:
        return _tool_error("list_snapshots", e)


@mcp.tool()
async def list_snapshot_files(
    repo_id: str,
    snapshot_id: str,
    path: str = "/",
) -> dict:
    """Browse files within a specific snapshot.

    Args:
        repo_id: The repository ID (as shown by list_snapshots / get_config). It is
                 resolved to the repo GUID that the Backrest ListSnapshotFiles API expects.
        snapshot_id: Snapshot ID to browse.
        path: Directory path within the snapshot to list. Defaults to root "/".

    Returns a list of file/directory entries at the given path.
    """
    client = get_client()
    try:
        validate_backrest_id("repo_id", repo_id)
        validate_backrest_id("snapshot_id", snapshot_id)
    except ValueError as e:
        return _tool_error("list_snapshot_files", e)
    t0 = time.perf_counter()
    try:
        repo_guid = await _resolve_repo_guid(client, repo_id)
        # SECURITY[deferred]: path not validated — goes in JSON body to local HTTP API; Backrest validates server-side.
        # Ticket: BKRST-2. Audit: 2026-06-04/backrest-mcp-2026-06.
        body = {"repoGuid": repo_guid, "snapshotId": snapshot_id, "path": path}
        raw = await client.post("ListSnapshotFiles", body)
        parsed = ListSnapshotFilesResponse.model_validate(raw)
        await emit_metric("backrest_tool", {"tool": "list_snapshot_files"}, {"duration_ms": (time.perf_counter() - t0) * 1000})
        return {"path": parsed.path, "entries": [e.model_dump(exclude_none=True) for e in parsed.entries]}
    except Exception as e:
        return _tool_error("list_snapshot_files", e)


@mcp.tool()
async def get_summary() -> dict:
    """Get the Backrest dashboard summary — 30-day stats per repo and plan.

    Returns per-repo and per-plan success/warning/failed backup counts, bytes
    scanned/added, total snapshots, and the next scheduled backup time.
    """
    client = get_client()
    t0 = time.perf_counter()
    try:
        raw = await client.post("GetSummaryDashboard", {})
        parsed = SummaryDashboard.model_validate(raw)
        await emit_metric("backrest_tool", {"tool": "get_summary"}, {"duration_ms": (time.perf_counter() - t0) * 1000})
        return parsed.model_dump(exclude_none=True)
    except Exception as e:
        return _tool_error("get_summary", e)


@mcp.tool()
async def get_operations(
    plan_id: Optional[str] = None,
    repo_id: Optional[str] = None,
    limit: int = 20,
) -> dict:
    """List recent backup operations, optionally filtered by plan or repo.

    Args:
        plan_id: Filter operations by plan ID.
        repo_id: Filter operations by repo ID (resolved to the repo GUID that the
                 operation selector matches on).
        limit: Maximum number of operations to return (default 20).

    Returns formatted operation lines with status icons, durations, and — when present —
    the operation ID and log reference (pass the log reference to get_logs).
    """
    client = get_client()
    try:
        if plan_id:
            validate_backrest_id("plan_id", plan_id)
        if repo_id:
            validate_backrest_id("repo_id", repo_id)
    except ValueError as e:
        return _tool_error("get_operations", e)

    t0 = time.perf_counter()
    try:
        # OpSelector matches repos on repo_guid, and GetOperations returns nothing without
        # a selector — so resolve repo filters to GUIDs and, when unfiltered, fan out
        # across all configured repos.
        base: dict = {}
        if plan_id:
            base["planId"] = plan_id
        if repo_id:
            selectors = [{**base, "repoGuid": await _resolve_repo_guid(client, repo_id)}]
        elif plan_id:
            selectors = [base]
        else:
            guids = [r["guid"] for r in await _list_repos(client) if r.get("guid")]
            selectors = [{"repoGuid": g} for g in guids] or [{}]

        ops: list = []
        for sel in selectors:
            body: dict = {"lastN": limit}
            if sel:
                body["selector"] = sel
            raw = await client.post("GetOperations", body)
            ops.extend(OperationList.model_validate(raw).operations)

        ops.sort(key=lambda o: o.unixTimeStartMs or 0, reverse=True)
        ops = ops[:limit]
        await emit_metric("backrest_tool", {"tool": "get_operations"}, {"duration_ms": (time.perf_counter() - t0) * 1000, "count": len(ops)})

        lines = []
        for op in ops:
            icon = _STATUS_ICONS.get(op.status or "", "?")
            ts = _fmt_ms(op.unixTimeStartMs)
            dur = _fmt_duration(op.unixTimeStartMs, op.unixTimeEndMs)
            plan = op.planId or "—"
            repo = op.repoId or "—"
            msg = op.displayMessage or ""
            line = f"{icon} #{op.id or '—'} [{ts}] plan={plan} repo={repo}"
            if dur:
                line += f" ({dur})"
            if msg:
                line += f" — {msg}"
            if op.logref:
                line += f" [log: {op.logref}]"
            lines.append(line)

        return {"operations": lines, "count": len(lines)}
    except Exception as e:
        return _tool_error("get_operations", e)


@mcp.tool()
async def get_logs(ref: str, max_bytes: int = 100_000) -> dict:
    """Fetch the log output for an operation.

    Args:
        ref: The operation's log reference — the "log:" value surfaced by get_operations.
        max_bytes: Cap on returned text size; when the log is larger, the tail is kept
                   (errors are usually at the end). Default 100000.

    Returns the decoded log text. Read-only.
    """
    try:
        validate_backrest_id("ref", ref)
    except ValueError as e:
        return _tool_error("get_logs", e)
    client = get_client()
    t0 = time.perf_counter()
    try:
        raw = await client.post_streaming("GetLogs", {"ref": ref})
        await emit_metric("backrest_tool", {"tool": "get_logs"}, {"duration_ms": (time.perf_counter() - t0) * 1000, "bytes": len(raw)})
        text = raw.decode("utf-8", "replace")
        truncated = len(text) > max_bytes
        if truncated:
            text = text[-max_bytes:]
        return {"ref": ref, "bytes": len(raw), "truncated": truncated, "log": text}
    except Exception as e:
        return _tool_error("get_logs", e)


@mcp.tool()
async def get_download_url(operation_id: str, file_path: str) -> dict:
    """Get a signed download URL for a file produced by a restore operation.

    Companion to restore_snapshot: after a restore, fetch a URL to download a specific
    restored file.

    Args:
        operation_id: The restore operation's ID (from get_operations).
        file_path: Path of the file within the restored output to download.

    Returns {"url": ...}. Read-only (produces a signed URL; writes nothing).
    """
    try:
        validate_backrest_id("operation_id", operation_id)
    except ValueError as e:
        return _tool_error("get_download_url", e)
    client = get_client()
    t0 = time.perf_counter()
    try:
        # op_id is an int64 proto field; connect JSON accepts a string-encoded int.
        # SECURITY[deferred]: file_path not validated — goes in JSON body to local HTTP API; Backrest validates server-side.
        # Ticket: BKRST-2. Audit: 2026-06-04/backrest-mcp-2026-06.
        raw = await client.post("GetDownloadURL", {"opId": operation_id, "filePath": file_path})
        await emit_metric("backrest_tool", {"tool": "get_download_url"}, {"duration_ms": (time.perf_counter() - t0) * 1000})
        return {"url": raw.get("value"), "operation_id": operation_id, "file_path": file_path}
    except Exception as e:
        return _tool_error("get_download_url", e)


# ---------------------------------------------------------------------------
# Write tools — registered only when BACKREST_READONLY=false
# ---------------------------------------------------------------------------

if not READONLY:
    @mcp.tool()
    async def trigger_backup(
        plan_id: str,
        dry_run: bool = False,
    ) -> dict:
        """Trigger a backup for a specific plan.

        Args:
            plan_id: The Backrest plan ID to back up.
            dry_run: If true, simulate the backup without writing data (default false).

        Returns the operation ID of the triggered backup.
        """
        try:
            validate_backrest_id("plan_id", plan_id)
        except ValueError as e:
            return _tool_error("trigger_backup", e)
        audit_log("trigger_backup", {"plan_id": plan_id, "dry_run": dry_run})
        client = get_client()
        body = {"value": plan_id, "dryRun": dry_run}
        t0 = time.perf_counter()
        try:
            result = await client.post("Backup", body)
            await emit_metric("backrest_tool", {"tool": "trigger_backup"}, {"duration_ms": (time.perf_counter() - t0) * 1000})
            return result
        except Exception as e:
            return _tool_error("trigger_backup", e)

    @mcp.tool()
    async def do_repo_task(
        repo_id: str,
        task: Literal["index", "prune", "check", "stats", "unlock", "forget"],
    ) -> dict:
        """Run a maintenance task on a Backrest repository.

        Args:
            repo_id: The Backrest repository ID.
            task: Task to run — one of: index, prune, check, stats, unlock, forget.
                  prune: Remove snapshots outside retention policy.
                  check: Verify repository integrity.
                  stats: Recalculate repository statistics.
                  unlock: Remove stale locks.
                  index: Re-index snapshot metadata.
                  forget: Apply forget/retention policy without pruning data.

        Returns the task result from Backrest.
        """
        try:
            validate_backrest_id("repo_id", repo_id)
        except ValueError as e:
            return _tool_error("do_repo_task", e)
        audit_log("do_repo_task", {"repo_id": repo_id, "task": task})
        client = get_client()
        task_int = _REPO_TASK_MAP[task]
        body = {"repoId": repo_id, "task": task_int}
        t0 = time.perf_counter()
        try:
            result = await client.post("DoRepoTask", body)
            await emit_metric("backrest_tool", {"tool": "do_repo_task"}, {"duration_ms": (time.perf_counter() - t0) * 1000})
            return result
        except Exception as e:
            return _tool_error("do_repo_task", e)

    @mcp.tool()
    async def cancel_operation(operation_id: str) -> dict:
        """Cancel a running Backrest operation.

        Args:
            operation_id: The operation ID to cancel (from get_operations).

        Returns the cancel result.
        """
        try:
            validate_backrest_id("operation_id", operation_id)
        except ValueError as e:
            return _tool_error("cancel_operation", e)
        audit_log("cancel_operation", {"operation_id": operation_id})
        client = get_client()
        t0 = time.perf_counter()
        try:
            result = await client.post("Cancel", {"value": operation_id})
            await emit_metric("backrest_tool", {"tool": "cancel_operation"}, {"duration_ms": (time.perf_counter() - t0) * 1000})
            return result
        except Exception as e:
            return _tool_error("cancel_operation", e)


# ---------------------------------------------------------------------------
# Destructive tools — registered only when ALLOW_DESTRUCTIVE=true
# ---------------------------------------------------------------------------

if ALLOW_DESTRUCTIVE:
    @mcp.tool()
    async def forget_snapshot(
        snapshot_id: str,
        repo_id: str,
        confirm: str,
        plan_id: Optional[str] = None,
    ) -> dict:
        """Forget (permanently delete) a specific snapshot from a Backrest repo.

        This is IRREVERSIBLE. The snapshot data will be removed on the next prune.

        Args:
            snapshot_id: The snapshot ID to forget.
            repo_id: The repository containing the snapshot.
            confirm: Must equal "FORGET:<snapshot_id>" to proceed.
                     Example: for snapshot_id "abc123", pass confirm="FORGET:abc123"
            plan_id: Optional plan ID for the forget operation.

        Returns the forget result, or an error if confirmation is wrong.
        """
        try:
            validate_backrest_id("snapshot_id", snapshot_id)
            validate_backrest_id("repo_id", repo_id)
        except ValueError as e:
            return _tool_error("forget_snapshot", e)
        expected = f"FORGET:{snapshot_id}"
        if confirm != expected:
            return {
                "content": [{"type": "text", "text": f'Confirmation required. Pass confirm="{expected}" to proceed.'}],
                "isError": True,
            }

        audit_log("forget_snapshot", {"snapshot_id": snapshot_id, "repo_id": repo_id, "plan_id": plan_id})
        client = get_client()
        body: dict = {"repoId": repo_id, "snapshotId": snapshot_id}
        if plan_id:
            body["planId"] = plan_id
        t0 = time.perf_counter()
        try:
            result = await client.post("Forget", body)
            await emit_metric("backrest_tool", {"tool": "forget_snapshot"}, {"duration_ms": (time.perf_counter() - t0) * 1000})
            return result
        except Exception as e:
            return _tool_error("forget_snapshot", e)

    @mcp.tool()
    async def restore_snapshot(
        snapshot_id: str,
        repo_id: str,
        path: str,
        target: str,
        plan_id: Optional[str] = None,
    ) -> dict:
        """Restore a snapshot (or a path within it) to a target directory.

        The target must be under BACKREST_RESTORE_ALLOWED_PREFIX (default: /tmp/backrest-restore/).
        This prevents accidental restores over live data. After verifying the restored files,
        move them to the desired location manually.

        WARNING: The target directory will be overwritten.

        Args:
            snapshot_id: Snapshot ID to restore from.
            repo_id: Repository containing the snapshot.
            path: Path within the snapshot to restore (e.g. "/home/ted/docs" or "/").
            target: Local filesystem path to restore to. Must be under the allowed prefix.
            plan_id: Optional plan ID.

        Returns the restore operation result, or an error if target is outside allowed prefix.
        """
        try:
            validate_backrest_id("snapshot_id", snapshot_id)
            validate_backrest_id("repo_id", repo_id)
        except ValueError as e:
            return _tool_error("restore_snapshot", e)
        allowed = pathlib.Path(os.path.realpath(RESTORE_ALLOWED_PREFIX))
        resolved = pathlib.Path(os.path.realpath(target))
        if not resolved.is_relative_to(allowed):
            return {
                "content": [{"type": "text", "text": (
                    f"Restore target must be under {RESTORE_ALLOWED_PREFIX}. "
                    f"Restore to a staging path, verify, then move manually."
                )}],
                "isError": True,
            }

        audit_log("restore_snapshot", {"snapshot_id": snapshot_id, "repo_id": repo_id, "path": path, "target": target, "plan_id": plan_id})
        client = get_client()
        # SECURITY[deferred]: path (within-snapshot source) not validated — goes in JSON body to local HTTP API; Backrest validates server-side.
        # Ticket: BKRST-2. Audit: 2026-06-04/backrest-mcp-2026-06.
        body: dict = {
            "snapshotId": snapshot_id,
            "repoId": repo_id,
            "path": path,
            "target": target,
        }
        if plan_id:
            body["planId"] = plan_id
        t0 = time.perf_counter()
        try:
            result = await client.post("Restore", body)
            await emit_metric("backrest_tool", {"tool": "restore_snapshot"}, {"duration_ms": (time.perf_counter() - t0) * 1000})
            return result
        except Exception as e:
            return _tool_error("restore_snapshot", e)


def main() -> None:
    from backrest_mcp.observability import configure_logging
    configure_logging()

    if TRANSPORT == "http":
        # HTTP transport adds a local network surface stdio never had. Fail closed:
        # loopback-only bind unless explicitly overridden, and a sufficiently long
        # bearer token is mandatory — never expose a reachable, unauthenticated port.
        if HTTP_HOST not in _LOOPBACK_HOSTS and not ALLOW_NONLOOPBACK:
            raise RuntimeError(
                f"Refusing to bind backrest-mcp HTTP transport to non-loopback host "
                f"{HTTP_HOST!r}. Set BACKREST_MCP_ALLOW_NONLOOPBACK=1 to override."
            )
        if not AUTH_TOKEN:
            raise RuntimeError(
                "Refusing to start backrest-mcp HTTP transport without BACKREST_MCP_AUTH_TOKEN "
                "set. HTTP mode must not run with an unauthenticated, reachable port."
            )
        if len(AUTH_TOKEN) < _MIN_AUTH_TOKEN_LENGTH:
            raise RuntimeError(
                f"BACKREST_MCP_AUTH_TOKEN is too short ({len(AUTH_TOKEN)} chars, need "
                f">= {_MIN_AUTH_TOKEN_LENGTH}). "
                'Generate one with: python3 -c "import secrets; print(secrets.token_hex(32))"'
            )
        log.info("backrest_mcp_http_start", host=HTTP_HOST, port=HTTP_PORT)
        mcp.run(transport="http", host=HTTP_HOST, port=HTTP_PORT)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
