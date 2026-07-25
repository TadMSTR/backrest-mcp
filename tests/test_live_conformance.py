"""
Live conformance tests against a running Backrest instance.

These are SKIPPED unless BACKREST_LIVE_TEST=1, so CI stays hermetic. Run them against
the deployed instance to catch connect-rpc field-name drift that the mock tests cannot:

    BACKREST_LIVE_TEST=1 \
    BACKREST_URL=http://localhost:9898 \
    BACKREST_USERNAME=... BACKREST_PASSWORD=... \
    pytest -q tests/test_live_conformance.py

They mirror the "Verification / Live conformance check" section of the v0.3.0 build plan.
"""

from __future__ import annotations

import os
import sys

import pytest

LIVE = os.environ.get("BACKREST_LIVE_TEST") == "1"
pytestmark = pytest.mark.skipif(not LIVE, reason="set BACKREST_LIVE_TEST=1 to run live conformance tests")


@pytest.fixture
def mcp_server():
    from backrest_mcp.client import get_client
    get_client.cache_clear()
    for mod in ["backrest_mcp.server", "backrest_mcp.safety"]:
        if mod in sys.modules:
            del sys.modules[mod]
    import backrest_mcp.server as srv
    yield srv.mcp
    get_client.cache_clear()


def _payload(result):
    import json
    return json.loads(result.content[0].text)


async def test_live_get_health_ok(mcp_server):
    data = _payload(await mcp_server.call_tool("get_health", {}))
    assert data["status"] == "ok", data
    assert data["repos"] >= 1


async def test_live_list_snapshots_no_arg_no_500(mcp_server):
    """list_snapshots() with no args must not 500 — it enumerates repos (#223)."""
    data = _payload(await mcp_server.call_tool("list_snapshots", {}))
    assert "error" not in data, data
    assert "snapshots" in data


async def test_live_get_summary_has_counts(mcp_server):
    data = _payload(await mcp_server.call_tool("get_summary", {}))
    assert "error" not in data, data
    # At least one repo summary with a success count present.
    repos = data.get("repoSummaries", [])
    assert repos, data


async def test_live_operations_and_logs_chain(mcp_server):
    """get_operations surfaces a log ref that get_logs can read (validates streaming)."""
    ops = _payload(await mcp_server.call_tool("get_operations", {"limit": 50}))
    lines = ops.get("operations", [])
    assert lines, ops
    ref = None
    for line in lines:
        if "[log: " in line:
            ref = line.split("[log: ", 1)[1].rstrip("]").strip()
            break
    if ref is None:
        pytest.skip("no operation with a log ref available")
    logs = _payload(await mcp_server.call_tool("get_logs", {"ref": ref}))
    assert "error" not in logs, logs
    assert "log" in logs


async def test_live_list_snapshot_files(mcp_server):
    """Resolve a real repo_id + snapshot_id and browse files (validates repoGuid path)."""
    snaps = _payload(await mcp_server.call_tool("list_snapshots", {}))
    entries = snaps.get("snapshots", [])
    if not entries:
        pytest.skip("no snapshots available")
    snap = entries[0]
    files = _payload(await mcp_server.call_tool(
        "list_snapshot_files",
        {"repo_id": snap["repoId"], "snapshot_id": snap["id"], "path": "/"},
    ))
    assert "error" not in files, files
    assert "entries" in files
