"""
Safety control unit tests.

Tests environment flag gating, tool registration, confirmation tokens,
restore path guards, and audit log output. No live Backrest instance needed.
"""

from __future__ import annotations

import json
import sys

import httpx
import pytest
import respx

BASE_URL = "http://localhost:9898"


def _body(route):
    return json.loads(route.calls[0].request.content)


def _reload_server(monkeypatch, readonly="true", destructive="false", restore_prefix=None):
    """Reload safety and server modules to pick up monkeypatched env vars."""
    monkeypatch.setenv("BACKREST_URL", BASE_URL)
    monkeypatch.setenv("BACKREST_READONLY", readonly)
    monkeypatch.setenv("BACKREST_ALLOW_DESTRUCTIVE", destructive)
    if restore_prefix:
        monkeypatch.setenv("BACKREST_RESTORE_ALLOWED_PREFIX", restore_prefix)
    for mod in ["backrest_mcp.safety", "backrest_mcp.server"]:
        if mod in sys.modules:
            del sys.modules[mod]
    import backrest_mcp.server as srv
    return srv.mcp


@pytest.fixture(autouse=True)
def reset_client_cache():
    from backrest_mcp.client import get_client
    get_client.cache_clear()
    yield
    get_client.cache_clear()


# ---------------------------------------------------------------------------
# Tool registration gating
# ---------------------------------------------------------------------------

async def test_readonly_true_no_write_tools(monkeypatch):
    """BACKREST_READONLY=true → write tools absent."""
    mcp = _reload_server(monkeypatch, readonly="true")
    tools = await mcp.list_tools()
    names = [t.name for t in tools]
    assert "trigger_backup" not in names
    assert "do_repo_task" not in names
    assert "cancel_operation" not in names
    assert "forget_snapshot" not in names
    assert "restore_snapshot" not in names


async def test_readonly_false_destructive_false_no_forget_restore(monkeypatch):
    """BACKREST_READONLY=false + ALLOW_DESTRUCTIVE=false → forget/restore absent."""
    mcp = _reload_server(monkeypatch, readonly="false", destructive="false")
    tools = await mcp.list_tools()
    names = [t.name for t in tools]
    assert "trigger_backup" in names
    assert "do_repo_task" in names
    assert "cancel_operation" in names
    assert "forget_snapshot" not in names
    assert "restore_snapshot" not in names


async def test_allow_destructive_true_all_tools_present(monkeypatch):
    """BACKREST_ALLOW_DESTRUCTIVE=true → forget/restore present."""
    mcp = _reload_server(monkeypatch, readonly="false", destructive="true")
    tools = await mcp.list_tools()
    names = [t.name for t in tools]
    assert "forget_snapshot" in names
    assert "restore_snapshot" in names


# ---------------------------------------------------------------------------
# forget_snapshot confirmation token
# ---------------------------------------------------------------------------

async def test_forget_wrong_confirm_returns_error(monkeypatch):
    """forget_snapshot with wrong confirm token → isError, no API call made."""
    mcp = _reload_server(monkeypatch, readonly="false", destructive="true")
    with respx.mock(assert_all_called=False) as mock:
        forget_route = mock.post(f"{BASE_URL}/v1.Backrest/Forget").mock(
            return_value=httpx.Response(200, json={})
        )
        result = await mcp.call_tool("forget_snapshot", {
            "snapshot_id": "abc123",
            "repo_id": "local",
            "confirm": "WRONG",
        })
        assert forget_route.call_count == 0
    text = result.content[0].text
    assert "FORGET:abc123" in text


async def test_forget_correct_confirm_calls_api(monkeypatch):
    """forget_snapshot with correct confirm token → API call made."""
    mcp = _reload_server(monkeypatch, readonly="false", destructive="true")
    with respx.mock() as mock:
        forget_route = mock.post(f"{BASE_URL}/v1.Backrest/Forget").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        await mcp.call_tool("forget_snapshot", {
            "snapshot_id": "abc123",
            "repo_id": "local",
            "confirm": "FORGET:abc123",
        })
        assert forget_route.call_count == 1


# ---------------------------------------------------------------------------
# restore_snapshot path guard
# ---------------------------------------------------------------------------

async def test_restore_outside_prefix_rejected(monkeypatch, tmp_path):
    """restore_snapshot with target outside allowed prefix → isError."""
    restore_prefix = str(tmp_path / "restore")
    mcp = _reload_server(monkeypatch, readonly="false", destructive="true", restore_prefix=restore_prefix)
    with respx.mock():
        result = await mcp.call_tool("restore_snapshot", {
            "snapshot_id": "abc123",
            "repo_id": "local",
            "path": "/home/ted",
            "target": "/etc/passwd",
        })
    text = result.content[0].text
    assert "must be under" in text


async def test_restore_path_traversal_rejected(monkeypatch, tmp_path):
    """restore_snapshot with path traversal attempt → isError (realpath check)."""
    restore_prefix = tmp_path / "restore"
    restore_prefix.mkdir()
    mcp = _reload_server(monkeypatch, readonly="false", destructive="true", restore_prefix=str(restore_prefix))
    traversal_target = str(restore_prefix / "../../etc")
    with respx.mock():
        result = await mcp.call_tool("restore_snapshot", {
            "snapshot_id": "abc123",
            "repo_id": "local",
            "path": "/",
            "target": traversal_target,
        })
    text = result.content[0].text
    assert "must be under" in text


async def test_restore_sibling_prefix_rejected(monkeypatch, tmp_path):
    """restore_snapshot with sibling directory name matching the prefix → isError (F-01)."""
    restore_prefix = tmp_path / "restore"
    restore_prefix.mkdir()
    sibling = tmp_path / "restore-evil"
    sibling.mkdir()
    mcp = _reload_server(monkeypatch, readonly="false", destructive="true", restore_prefix=str(restore_prefix))
    with respx.mock():
        result = await mcp.call_tool("restore_snapshot", {
            "snapshot_id": "abc123",
            "repo_id": "local",
            "path": "/",
            "target": str(sibling / "data"),
        })
    text = result.content[0].text
    assert "must be under" in text


async def test_restore_valid_target_calls_api(monkeypatch, tmp_path):
    """restore_snapshot with valid target inside prefix → API call made."""
    restore_prefix = tmp_path / "restore"
    restore_prefix.mkdir()
    mcp = _reload_server(monkeypatch, readonly="false", destructive="true", restore_prefix=str(restore_prefix))
    with respx.mock() as mock:
        restore_route = mock.post(f"{BASE_URL}/v1.Backrest/Restore").mock(
            return_value=httpx.Response(200, json={"operationId": "op-restore-1"})
        )
        await mcp.call_tool("restore_snapshot", {
            "snapshot_id": "abc123",
            "repo_id": "local",
            "path": "/home/ted/docs",
            "target": str(restore_prefix / "docs"),
        })
        assert restore_route.call_count == 1


# ---------------------------------------------------------------------------
# Write tool happy paths (BACKREST_READONLY=false)
# ---------------------------------------------------------------------------

async def test_trigger_backup_calls_api(monkeypatch):
    mcp = _reload_server(monkeypatch, readonly="false")
    with respx.mock() as mock:
        route = mock.post(f"{BASE_URL}/v1.Backrest/Backup").mock(
            return_value=httpx.Response(200, json={})
        )
        await mcp.call_tool("trigger_backup", {"plan_id": "forge-system", "dry_run": True})
    body = _body(route)
    assert body["value"] == "forge-system"
    assert body["dryRun"] is True


async def test_do_repo_task_maps_task_enum(monkeypatch):
    mcp = _reload_server(monkeypatch, readonly="false")
    with respx.mock() as mock:
        route = mock.post(f"{BASE_URL}/v1.Backrest/DoRepoTask").mock(
            return_value=httpx.Response(200, json={})
        )
        await mcp.call_tool("do_repo_task", {"repo_id": "atlas-forge", "task": "prune"})
    body = _body(route)
    assert body["repoId"] == "atlas-forge"
    assert body["task"] == 2  # TASK_PRUNE


async def test_cancel_operation_calls_api(monkeypatch):
    mcp = _reload_server(monkeypatch, readonly="false")
    with respx.mock() as mock:
        route = mock.post(f"{BASE_URL}/v1.Backrest/Cancel").mock(
            return_value=httpx.Response(200, json={})
        )
        await mcp.call_tool("cancel_operation", {"operation_id": "396"})
    assert _body(route)["value"] == "396"


async def test_trigger_backup_invalid_plan_rejected(monkeypatch):
    mcp = _reload_server(monkeypatch, readonly="false")
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(f"{BASE_URL}/v1.Backrest/Backup").mock(
            return_value=httpx.Response(200, json={})
        )
        result = await mcp.call_tool("trigger_backup", {"plan_id": "bad;rm -rf"})
    assert route.call_count == 0
    assert "error" in result.content[0].text


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

async def test_audit_log_writes_entry(monkeypatch, tmp_path):
    """audit_log writes a JSONL entry when AUDIT_LOG is set."""
    audit_file = tmp_path / "audit.jsonl"
    # Reload safety with the env var set
    if "backrest_mcp.safety" in sys.modules:
        del sys.modules["backrest_mcp.safety"]
    monkeypatch.setenv("BACKREST_AUDIT_LOG", str(audit_file))
    import backrest_mcp.safety as safety
    # Patch the module-level constant to ensure it's picked up
    safety.AUDIT_LOG = str(audit_file)
    safety.audit_log("trigger_backup", {"plan_id": "daily", "dry_run": False})
    assert audit_file.exists(), "audit log file was not created"
    line = audit_file.read_text().strip()
    entry = json.loads(line)
    assert entry["tool"] == "trigger_backup"
    assert entry["args"]["plan_id"] == "daily"
    assert "ts" in entry
