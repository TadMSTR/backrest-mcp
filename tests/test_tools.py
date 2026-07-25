"""
End-to-end tool tests using FastMCP 3.x direct call API.

Tests the read-only tools (always registered). Write/destructive tools are
covered in test_safety.py since they depend on env var gating.

Mocks follow the deployed Backrest v1.13.0 connect-rpc field names.
"""

from __future__ import annotations

import base64
import json
import struct
import sys

import httpx
import pytest
import respx

BASE_URL = "http://localhost:9898"
REPO_ID = "atlas-forge"
REPO_GUID = "1490f01ca17f16e22e704c50e5a78b5728abc57a7617f08f19a76a04ecac574e"


@pytest.fixture(autouse=True)
def reset_client_cache():
    from backrest_mcp.client import get_client
    get_client.cache_clear()
    yield
    get_client.cache_clear()


@pytest.fixture
def mock_env(monkeypatch):
    monkeypatch.setenv("BACKREST_URL", BASE_URL)
    monkeypatch.setenv("BACKREST_USERNAME", "")
    monkeypatch.setenv("BACKREST_PASSWORD", "")
    monkeypatch.setenv("BACKREST_READONLY", "true")
    monkeypatch.setenv("BACKREST_ALLOW_DESTRUCTIVE", "false")


@pytest.fixture
def mcp_server(mock_env):
    """Return a fresh FastMCP server instance with env vars applied."""
    for mod in ["backrest_mcp.server", "backrest_mcp.safety"]:
        if mod in sys.modules:
            del sys.modules[mod]
    import backrest_mcp.server as srv
    return srv.mcp


def _config_response(repos=None):
    return {
        "version": 6,
        "repos": repos if repos is not None else [{"id": REPO_ID, "guid": REPO_GUID}],
        "plans": [{"id": "forge-system"}],
    }


def _connect_stream(chunks):
    """Encode text chunks as a Connect server-streaming response of BytesValue frames."""
    out = bytearray()
    for text in chunks:
        payload = json.dumps({"value": base64.b64encode(text.encode()).decode()}).encode()
        out += struct.pack(">BI", 0, len(payload)) + payload
    end = b"{}"
    out += struct.pack(">BI", 0b00000010, len(end)) + end
    return bytes(out)


# ---------------------------------------------------------------------------
# get_health
# ---------------------------------------------------------------------------

async def test_get_health_ok(mcp_server):
    with respx.mock() as mock:
        mock.post(f"{BASE_URL}/v1.Backrest/GetConfig").mock(
            return_value=httpx.Response(200, json=_config_response())
        )
        result = await mcp_server.call_tool("get_health", {})
    text = result.content[0].text
    assert '"ok"' in text or "ok" in text
    assert REPO_ID not in text  # health does not leak repo ids, only counts
    assert '"repos": 1' in text or '"repos":1' in text


async def test_get_health_auth_failed(mcp_server):
    with respx.mock() as mock:
        mock.post(f"{BASE_URL}/v1.Backrest/GetConfig").mock(
            return_value=httpx.Response(401, json={"message": "Unauthorized"})
        )
        result = await mcp_server.call_tool("get_health", {})
    assert "auth_failed" in result.content[0].text


async def test_get_health_unreachable(mcp_server):
    with respx.mock() as mock:
        mock.post(f"{BASE_URL}/v1.Backrest/GetConfig").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        result = await mcp_server.call_tool("get_health", {})
    assert "unreachable" in result.content[0].text


# ---------------------------------------------------------------------------
# get_summary — real field names
# ---------------------------------------------------------------------------

async def test_get_summary_returns_failed_and_warning_counts(mcp_server):
    """get_summary surfaces backupsFailed30days / warning counts (bug #4 fix)."""
    summary_response = {
        "repoSummaries": [
            {
                "id": REPO_ID,
                "backupsSuccessLast30days": 28,
                "backupsFailed30days": 2,
                "backupsWarningLast30days": 1,
                "bytesScannedLast30days": 2048,
                "bytesAddedLast30days": 1048576,
                "totalSnapshots": 120,
            }
        ],
        "planSummaries": [],
    }
    with respx.mock() as mock:
        mock.post(f"{BASE_URL}/v1.Backrest/GetSummaryDashboard").mock(
            return_value=httpx.Response(200, json=summary_response)
        )
        result = await mcp_server.call_tool("get_summary", {})

    assert not result.is_error
    text = result.content[0].text
    assert "backupsFailed30days" in text
    assert "backupsWarningLast30days" in text


# ---------------------------------------------------------------------------
# get_operations — selector resolution + logref surfacing
# ---------------------------------------------------------------------------

async def test_get_operations_no_filter_enumerates_repos(mcp_server):
    """No filter → enumerate repos and query GetOperations per repo GUID."""
    ops_response = {
        "operations": [
            {
                "id": "396",
                "planId": "forge-system",
                "repoId": REPO_ID,
                "repoGuid": REPO_GUID,
                "status": "STATUS_SUCCESS",
                "unixTimeStartMs": 1748000000000,
                "unixTimeEndMs": 1748000060000,
                "displayMessage": "Backup complete",
                "logref": "t-361bc3f1",
            }
        ]
    }
    with respx.mock() as mock:
        mock.post(f"{BASE_URL}/v1.Backrest/GetConfig").mock(
            return_value=httpx.Response(200, json=_config_response())
        )
        ops_route = mock.post(f"{BASE_URL}/v1.Backrest/GetOperations").mock(
            return_value=httpx.Response(200, json=ops_response)
        )
        result = await mcp_server.call_tool("get_operations", {"limit": 5})

    body = json.loads(ops_route.calls[0].request.content)
    assert body["selector"]["repoGuid"] == REPO_GUID
    text = result.content[0].text
    assert "✓" in text
    assert "t-361bc3f1" in text  # logref surfaced for chaining to get_logs
    assert "#396" in text


async def test_get_operations_repo_filter_resolves_guid(mcp_server):
    """repo_id filter is resolved to a repoGuid selector."""
    with respx.mock() as mock:
        mock.post(f"{BASE_URL}/v1.Backrest/GetConfig").mock(
            return_value=httpx.Response(200, json=_config_response())
        )
        ops_route = mock.post(f"{BASE_URL}/v1.Backrest/GetOperations").mock(
            return_value=httpx.Response(200, json={"operations": []})
        )
        await mcp_server.call_tool("get_operations", {"repo_id": REPO_ID, "limit": 5})

    body = json.loads(ops_route.calls[0].request.content)
    assert body["selector"]["repoGuid"] == REPO_GUID
    assert "repoId" not in body["selector"]


# ---------------------------------------------------------------------------
# list_snapshots — no-arg enumeration (#223)
# ---------------------------------------------------------------------------

async def test_list_snapshots_no_arg_enumerates_and_tags_repo(mcp_server):
    """list_snapshots() with no args enumerates repos and tags each snapshot (#223)."""
    with respx.mock() as mock:
        mock.post(f"{BASE_URL}/v1.Backrest/GetConfig").mock(
            return_value=httpx.Response(200, json=_config_response())
        )
        mock.post(f"{BASE_URL}/v1.Backrest/ListSnapshots").mock(
            return_value=httpx.Response(200, json={"snapshots": [{"id": "snap1"}]})
        )
        result = await mcp_server.call_tool("list_snapshots", {})

    text = result.content[0].text
    assert "snap1" in text
    assert REPO_ID in text  # snapshot tagged with its repoId


# ---------------------------------------------------------------------------
# list_snapshot_files — repo_id → guid resolution (v1.13.0 uses repoGuid)
# ---------------------------------------------------------------------------

async def test_list_snapshot_files_sends_repo_guid(mcp_server):
    """list_snapshot_files resolves repo_id and sends repoGuid to the API."""
    with respx.mock() as mock:
        mock.post(f"{BASE_URL}/v1.Backrest/GetConfig").mock(
            return_value=httpx.Response(200, json=_config_response())
        )
        lsf_route = mock.post(f"{BASE_URL}/v1.Backrest/ListSnapshotFiles").mock(
            return_value=httpx.Response(200, json={"path": "/", "entries": [{"name": "home", "type": "dir"}]})
        )
        result = await mcp_server.call_tool("list_snapshot_files", {"repo_id": REPO_ID, "snapshot_id": "snap1"})

    body = json.loads(lsf_route.calls[0].request.content)
    assert body["repoGuid"] == REPO_GUID
    assert "repoId" not in body
    assert "home" in result.content[0].text


async def test_list_snapshot_files_unknown_repo_errors(mcp_server):
    """Unknown repo_id → resolution fails → tool error, no ListSnapshotFiles call."""
    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{BASE_URL}/v1.Backrest/GetConfig").mock(
            return_value=httpx.Response(200, json=_config_response(repos=[]))
        )
        lsf_route = mock.post(f"{BASE_URL}/v1.Backrest/ListSnapshotFiles").mock(
            return_value=httpx.Response(200, json={})
        )
        result = await mcp_server.call_tool("list_snapshot_files", {"repo_id": "nope", "snapshot_id": "snap1"})

    assert lsf_route.call_count == 0
    assert "error" in result.content[0].text


# ---------------------------------------------------------------------------
# get_logs — Connect server-streaming
# ---------------------------------------------------------------------------

async def test_get_logs_decodes_stream(mcp_server):
    """get_logs decodes Connect BytesValue frames into text."""
    with respx.mock() as mock:
        mock.post(f"{BASE_URL}/v1.Backrest/GetLogs").mock(
            return_value=httpx.Response(200, content=_connect_stream(["line one\n", "line two\n"]))
        )
        result = await mcp_server.call_tool("get_logs", {"ref": "t-361bc3f1"})

    text = result.content[0].text
    assert "line one" in text
    assert "line two" in text


async def test_get_logs_rejects_bad_ref(mcp_server):
    """get_logs validates the ref before any network call."""
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(f"{BASE_URL}/v1.Backrest/GetLogs").mock(
            return_value=httpx.Response(200, content=b"")
        )
        result = await mcp_server.call_tool("get_logs", {"ref": "bad ref; rm -rf"})
    assert route.call_count == 0
    assert "error" in result.content[0].text


# ---------------------------------------------------------------------------
# get_download_url
# ---------------------------------------------------------------------------

async def test_get_download_url_returns_url(mcp_server):
    with respx.mock() as mock:
        route = mock.post(f"{BASE_URL}/v1.Backrest/GetDownloadURL").mock(
            return_value=httpx.Response(200, json={"value": "http://localhost:9898/download/abc"})
        )
        result = await mcp_server.call_tool("get_download_url", {"operation_id": "396", "file_path": "/home/ted/x"})

    body = json.loads(route.calls[0].request.content)
    assert body["opId"] == "396"
    assert body["filePath"] == "/home/ted/x"
    assert "download/abc" in result.content[0].text


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

async def test_trigger_backup_absent_in_readonly(mcp_server):
    """trigger_backup is not registered when BACKREST_READONLY=true."""
    tools = await mcp_server.list_tools()
    names = [t.name for t in tools]
    assert "trigger_backup" not in names


async def test_readonly_tools_always_present(mcp_server):
    """Read-only tools are always registered regardless of safety flags."""
    tools = await mcp_server.list_tools()
    names = [t.name for t in tools]
    for expected in [
        "get_health", "get_config", "list_snapshots", "list_snapshot_files",
        "get_summary", "get_operations", "get_logs", "get_download_url",
    ]:
        assert expected in names, f"{expected} missing from tool list"
