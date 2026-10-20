"""
Live conformance tests against a running Backrest instance.

These are SKIPPED unless BACKREST_LIVE_TEST=1, so CI stays hermetic. Run them against
the deployed instance to catch connect-rpc field-name drift that the mock tests cannot:

    python scripts/run_live_conformance.py

The wrapper reads the BACKREST_* credentials it needs from the file named by
BACKREST_LIVE_ENV_FILE (default ~/.secrets/forge.env) inside the process, so no credential
is ever on a command line, and runs this file with ``-p no:cacheprovider --tb=line``.
``--tb=line`` keeps traceback locals (which can hold config data) out of the output.

Every secret check is computed first and asserted as a bool with a fixed message: pytest's
assertion rewriting prints both operands of a failing comparison.

They mirror the "Verification / Live conformance check" section of the v0.3.0 build plan.
"""

from __future__ import annotations

import json
import os
import sys
import warnings

import pytest

LIVE = os.environ.get("BACKREST_LIVE_TEST") == "1"
pytestmark = pytest.mark.skipif(
    not LIVE, reason="set BACKREST_LIVE_TEST=1 to run live conformance tests"
)


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
    files = _payload(
        await mcp_server.call_tool(
            "list_snapshot_files",
            {"repo_id": snap["repoId"], "snapshot_id": snap["id"], "path": "/"},
        )
    )
    assert "error" not in files, files
    assert "entries" in files


async def test_live_get_config_omits_repo_password(mcp_server):
    """get_config must never return the restic repo password (#1176).

    Reads the expected secret from BACKREST_REPO_PASSWORD; skipped when unset. The assert
    is written so that a failure cannot print the secret: pytest's assertion rewriting
    shows both operands, so compare first and assert on the bool with a fixed message.
    Run this file with ``-p no:cacheprovider --tb=line``.
    """
    import json

    secret = os.environ.get("BACKREST_REPO_PASSWORD", "")
    if not secret:
        pytest.skip("BACKREST_REPO_PASSWORD not set")
    raw = (await mcp_server.call_tool("get_config", {})).content[0].text
    errored = "error" in json.loads(raw)
    leaked = _contains_secret(raw, secret)
    del raw, secret
    assert not errored, "get_config returned an error"
    assert not leaked, "repo password present in get_config output"


def _secret_forms(secret: str) -> set[str]:
    """The secret as it could appear inside JSON text: raw, ASCII-escaped, UTF-8 escaped.

    A password with a quote, backslash, control or non-ASCII character is escaped in the
    tool's JSON output, so a plain substring check would miss it (audit p2 F-04).
    """
    return {
        secret,
        json.dumps(secret)[1:-1],
        json.dumps(secret, ensure_ascii=False)[1:-1],
    }


def _contains_secret(raw: str, secret: str) -> bool:
    return bool(secret) and any(form in raw for form in _secret_forms(secret))


def _secret_free(raw: str) -> tuple[bool, bool]:
    """(errored, leaked) for a tool's raw JSON text. Quoted-key matches, never bare `env`."""
    secret = os.environ.get("BACKREST_REPO_PASSWORD", "")
    errored = "error" in json.loads(raw)
    leaked = _contains_secret(raw, secret) or '"password"' in raw or '"env"' in raw
    return errored, leaked


async def test_live_list_plans(mcp_server):
    raw = (await mcp_server.call_tool("list_plans", {})).content[0].text
    errored, leaked = _secret_free(raw)
    ok_shape = not errored and json.loads(raw).get("count", 0) >= 1
    del raw
    assert not errored, "list_plans returned an error"
    assert not leaked, "list_plans output carries a password or env key"
    assert ok_shape, "list_plans returned no plans"


async def test_live_find_plan_for_path(mcp_server):
    raw = (await mcp_server.call_tool("find_plan_for_path", {"path": "/home/ted"})).content[0].text
    errored, leaked = _secret_free(raw)
    data = {} if errored else json.loads(raw)
    del raw
    assert not errored, "find_plan_for_path returned an error"
    assert not leaked, "find_plan_for_path output carries a password or env key"
    ok_shape = data.get("match") == "approximate" and isinstance(data.get("plans"), list)
    assert ok_shape, "find_plan_for_path response has the wrong shape"


async def test_live_get_backup_freshness(mcp_server):
    raw = (await mcp_server.call_tool("get_backup_freshness", {})).content[0].text
    errored, leaked = _secret_free(raw)
    data = {} if errored else json.loads(raw)
    plans = data.get("plans", [])
    del raw
    assert not errored, "get_backup_freshness returned an error"
    assert not leaked, "get_backup_freshness output carries a password or env key"
    states = {p.get("state") for p in plans}
    ok_shape = (
        bool(plans)
        and states <= {"ok", "stale", "unscheduled"}
        and isinstance(data.get("stale_count"), int)
        and isinstance(data.get("unscheduled_count"), int)
    )
    assert ok_shape, "get_backup_freshness returned no plans or an unknown state"


async def test_live_policy_messages_have_no_unknown_fields():
    """Warn, by key name only, when Backrest's nested policy messages grow a field (F-04).

    redact.py drops an unlisted field, so this is not a leak; it is the prompt to decide
    whether the new field belongs in the allowlist. Values are never printed.
    """
    from backrest_mcp.client import get_client
    from backrest_mcp.redact import unknown_policy_keys

    get_client.cache_clear()
    unknown = unknown_policy_keys(await get_client().post("GetConfig", {}))
    get_client.cache_clear()
    if unknown:
        warnings.warn(
            "Backrest config has policy fields redact.py does not list (dropped): "
            + ", ".join(sorted(set(unknown))),
            stacklevel=1,
        )
