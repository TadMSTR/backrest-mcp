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


def _config_with_secrets():
    """A v1.13.0-shaped config with a secret in every field that can hold one.

    Every secret value contains LEAKCANARY, so one substring check catches a field the
    projection forgot (#1176).
    """
    hook_actions = {
        "actionCommand": {"command": "curl -H 'X-Token: LEAKCANARY-cmd' https://h/"},
        "actionWebhook": {
            "webhookUrl": "https://h/hook/LEAKCANARY-webhook",
            "method": "POST",
            "template": "LEAKCANARY-tpl",
        },
        "actionDiscord": {"webhookUrl": "https://discord.com/api/webhooks/LEAKCANARY-discord"},
        "actionGotify": {"baseUrl": "https://gotify/", "token": "LEAKCANARY-gotify"},
        "actionSlack": {"webhookUrl": "https://hooks.slack.com/LEAKCANARY-slack"},
        "actionShoutrrr": {"shoutrrrUrl": "telegram://LEAKCANARY-shoutrrr@telegram"},
        "actionHealthchecks": {"webhookUrl": "https://hc/ping/LEAKCANARY-hc"},
        "actionTelegram": {"botToken": "LEAKCANARY-telegram", "chatId": "1"},
    }
    return {
        "modno": 42,
        "version": 6,
        "instance": "forge",
        "repos": [
            {
                "id": REPO_ID,
                "guid": REPO_GUID,
                "uri": "rest:https://u:LEAKCANARY-uri@host/repo",
                "password": "LEAKCANARY-repo-password",
                "env": [
                    "AWS_ACCESS_KEY_ID=LEAKCANARY-akid",
                    "AWS_SECRET_ACCESS_KEY=LEAKCANARY-aws",
                    "B2_ACCOUNT_KEY=LEAKCANARY-b2",
                ],
                "flags": ["--password-command=echo LEAKCANARY-flag", "-o", "s3.key=LEAKCANARY-o"],
                "prunePolicy": {"schedule": {"cron": "0 3 * * 0"}, "maxUnusedPercent": 10},
                "checkPolicy": {"schedule": {"maxFrequencyDays": 30}, "structureOnly": True},
                "autoUnlock": True,
                "hooks": [
                    {"conditions": ["CONDITION_ANY_ERROR"], "onError": "ON_ERROR_IGNORE", k: v}
                    for k, v in hook_actions.items()
                ],
                "futureSecretField": "LEAKCANARY-future",
            }
        ],
        "plans": [
            {
                "id": "forge-system",
                "repo": REPO_ID,
                "paths": ["/home/ted/Documents", "/etc"],
                "excludes": ["*.tmp"],
                "schedule": {"cron": "0 2 * * *", "clock": "CLOCK_LOCAL"},
                "retention": {"policyTimeBucketed": {"daily": 7, "weekly": 4}},
                "backup_flags": ["--exclude-caches", "--password-file=LEAKCANARY-bflag"],
                "hooks": [
                    {
                        "conditions": ["CONDITION_SNAPSHOT_SUCCESS"],
                        "actionHealthchecks": {"webhookUrl": "https://hc/ping/LEAKCANARY-plan-hc"},
                    }
                ],
            }
        ],
        "auth": {
            "disabled": False,
            "users": [{"name": "ted", "passwordBcrypt": "LEAKCANARY-bcrypt"}],
        },
        "sync": {
            "identity": {"keyid": "k", "ed25519priv": "LEAKCANARY-identity"},
            "pairingTokens": [{"secret": "LEAKCANARY-pairing", "label": "l"}],
            "knownHosts": [{"instanceId": "atlas", "initialPairingSecret": "LEAKCANARY-known"}],
        },
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
# get_config — allowlist projection (#1176)
# ---------------------------------------------------------------------------


async def _get_config(mcp_server, cfg):
    with respx.mock() as mock:
        mock.post(f"{BASE_URL}/v1.Backrest/GetConfig").mock(
            return_value=httpx.Response(200, json=cfg)
        )
        result = await mcp_server.call_tool("get_config", {})
    return json.loads(result.content[0].text)


async def test_get_config_leaks_no_secret(mcp_server):
    """Load-bearing: any secret field that reaches the output carries LEAKCANARY."""
    data = await _get_config(mcp_server, _config_with_secrets())
    assert "LEAKCANARY" not in json.dumps(data)


async def test_get_config_keeps_useful_fields(mcp_server):
    """The projection is not an empty dict: the fields agents need survive."""
    data = await _get_config(mcp_server, _config_with_secrets())
    assert data["version"] == 6
    assert data["instance"] == "forge"
    repo = data["repos"][0]
    assert repo["id"] == REPO_ID
    assert repo["guid"] == REPO_GUID
    assert repo["uri"] == "rest:https://host/repo"
    assert repo["envKeys"] == ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "B2_ACCOUNT_KEY"]
    assert repo["hasPassword"] is True
    assert repo["flagCount"] == 3
    assert repo["prunePolicy"]["maxUnusedPercent"] == 10
    assert [h["action"] for h in repo["hooks"]] == [
        "actionCommand",
        "actionWebhook",
        "actionDiscord",
        "actionGotify",
        "actionSlack",
        "actionShoutrrr",
        "actionHealthchecks",
        "actionTelegram",
    ]
    assert repo["hooks"][0] == {
        "conditions": ["CONDITION_ANY_ERROR"],
        "onError": "ON_ERROR_IGNORE",
        "action": "actionCommand",
    }
    plan = data["plans"][0]
    assert plan["paths"] == ["/home/ted/Documents", "/etc"]
    assert plan["schedule"] == {"cron": "0 2 * * *", "clock": "CLOCK_LOCAL"}
    assert plan["retention"] == {"policyTimeBucketed": {"daily": 7, "weekly": 4}}
    assert plan["backupFlagCount"] == 2
    assert plan["hooks"][0]["action"] == "actionHealthchecks"
    assert data["auth"] == {"disabled": False, "users": [{"name": "ted"}]}


async def test_get_config_drops_unknown_and_secret_keys(mcp_server):
    """Keys not on the allowlist are absent, including ones Backrest does not have yet."""
    data = await _get_config(mcp_server, _config_with_secrets())
    repo = data["repos"][0]
    for key in ("futureSecretField", "password", "env", "flags"):
        assert key not in repo
    assert "backup_flags" not in data["plans"][0]
    assert "sync" not in data
    assert "passwordBcrypt" not in data["auth"]["users"][0]


async def test_get_config_backend_error_is_generic(mcp_server):
    with respx.mock() as mock:
        mock.post(f"{BASE_URL}/v1.Backrest/GetConfig").mock(return_value=httpx.Response(500))
        result = await mcp_server.call_tool("get_config", {})
    assert json.loads(result.content[0].text) == {
        "error": "tool call failed — check server logs for details"
    }


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
            return_value=httpx.Response(
                200, json={"path": "/", "entries": [{"name": "home", "type": "dir"}]}
            )
        )
        result = await mcp_server.call_tool(
            "list_snapshot_files", {"repo_id": REPO_ID, "snapshot_id": "snap1"}
        )

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
        result = await mcp_server.call_tool(
            "list_snapshot_files", {"repo_id": "nope", "snapshot_id": "snap1"}
        )

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
        result = await mcp_server.call_tool(
            "get_download_url", {"operation_id": "396", "file_path": "/home/ted/x"}
        )

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
    """Read-only mode registers exactly the read tools: none missing, nothing extra."""
    tools = await mcp_server.list_tools()
    assert {t.name for t in tools} == {
        "get_health",
        "get_config",
        "list_plans",
        "find_plan_for_path",
        "get_backup_freshness",
        "list_snapshots",
        "list_snapshot_files",
        "get_summary",
        "get_operations",
        "get_logs",
        "get_download_url",
    }


# ---------------------------------------------------------------------------
# Plan tools: list_plans, find_plan_for_path, get_backup_freshness
# ---------------------------------------------------------------------------

NOW_MS = 1_760_000_000_000
HOUR_MS = 3_600_000


def _no_secret(text: str) -> bool:
    """Secret-absence by quoted JSON key, never bare substrings (`env` matches `envKeys`)."""
    return "LEAKCANARY" not in text and '"password"' not in text and '"env"' not in text


async def _call(mcp_server, tool, args, cfg, ops_by_plan=None):
    """Call a tool with GetConfig -> cfg and GetOperations answered per selector.planId."""
    ops_by_plan = ops_by_plan or {}

    def operations(request):
        sel = json.loads(request.content).get("selector", {})
        return httpx.Response(200, json={"operations": ops_by_plan.get(sel.get("planId"), [])})

    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{BASE_URL}/v1.Backrest/GetConfig").mock(
            return_value=httpx.Response(200, json=cfg)
        )
        ops_route = mock.post(f"{BASE_URL}/v1.Backrest/GetOperations").mock(side_effect=operations)
        result = await mcp_server.call_tool(tool, args)
    return result.content[0].text, ops_route


async def test_list_plans_projects_and_leaks_nothing(mcp_server):
    text, _ = await _call(mcp_server, "list_plans", {}, _config_with_secrets())
    assert _no_secret(text)
    data = json.loads(text)
    assert data["count"] == 1
    plan = data["plans"][0]
    assert plan["id"] == "forge-system"
    assert plan["paths"] == ["/home/ted/Documents", "/etc"]
    assert plan["backupFlagCount"] == 2
    assert "backup_flags" not in plan


@pytest.mark.parametrize(
    ("path", "backed_up", "excluded"),
    [
        ("/home/ted/Documents/notes.md", True, None),
        ("/home/ted/Documents", True, None),
        ("/etc/hosts", True, None),
        ("/home/ted/Documents/scratch.tmp", False, "*.tmp"),
    ],
)
async def test_find_plan_for_path_covered(mcp_server, path, backed_up, excluded):
    text, _ = await _call(mcp_server, "find_plan_for_path", {"path": path}, _config_with_secrets())
    assert _no_secret(text)
    data = json.loads(text)
    assert data["match"] == "approximate"
    assert data["backed_up"] is backed_up
    [match] = data["plans"]
    assert match["plan_id"] == "forge-system"
    assert match["repo"] == REPO_ID
    assert match["excluded_by"] == excluded
    assert match["schedule"] == {"cron": "0 2 * * *", "clock": "CLOCK_LOCAL"}


async def test_find_plan_for_path_not_covered(mcp_server):
    text, _ = await _call(
        mcp_server, "find_plan_for_path", {"path": "/var/lib/x"}, _config_with_secrets()
    )
    data = json.loads(text)
    assert data == {"path": "/var/lib/x", "match": "approximate", "backed_up": False, "plans": []}


@pytest.mark.parametrize("path", ["relative/x", "", "/a\x00b", "/" + "a" * 5000])
async def test_find_plan_for_path_rejects_bad_path(mcp_server, path):
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(f"{BASE_URL}/v1.Backrest/GetConfig")
        result = await mcp_server.call_tool("find_plan_for_path", {"path": path})
    assert "error" in json.loads(result.content[0].text)
    assert route.call_count == 0


def _freshness_config(schedule):
    cfg = _config_with_secrets()
    cfg["plans"][0]["schedule"] = schedule
    return cfg


def _backup(status, end_hours_ago, kind="operationBackup"):
    end = NOW_MS - int(end_hours_ago * HOUR_MS)
    return {
        "id": str(end),
        "planId": "forge-system",
        "status": status,
        "unixTimeStartMs": str(end - 60_000),
        "unixTimeEndMs": str(end),
        kind: {},
    }


@pytest.fixture
def frozen_now(mcp_server, monkeypatch):
    srv = sys.modules["backrest_mcp.server"]
    monkeypatch.setattr(srv, "_now", lambda: NOW_MS / 1000)
    return mcp_server


async def _freshness(mcp, schedule, ops, args=None):
    text, route = await _call(
        mcp, "get_backup_freshness", args or {}, _freshness_config(schedule), {"forge-system": ops}
    )
    assert _no_secret(text)
    return json.loads(text), route


@pytest.mark.parametrize(
    ("schedule", "hours_ago", "state"),
    [
        # cron: 26 h default, stale only once strictly older
        ({"cron": "0 2 * * *"}, 25.99, "ok"),
        ({"cron": "0 2 * * *"}, 26.0, "ok"),
        ({"cron": "0 2 * * *"}, 26.01, "stale"),
        # maxFrequencyHours 6 -> 12 h
        ({"maxFrequencyHours": 6}, 11.99, "ok"),
        ({"maxFrequencyHours": 6}, 12.01, "stale"),
        # maxFrequencyDays 1 -> 48 h
        ({"maxFrequencyDays": 1}, 47.99, "ok"),
        ({"maxFrequencyDays": 1}, 48.01, "stale"),
        # disabled is never stale, however old
        ({"disabled": True}, 10_000, "unscheduled"),
    ],
)
async def test_freshness_threshold_boundaries(frozen_now, schedule, hours_ago, state):
    data, _ = await _freshness(frozen_now, schedule, [_backup("STATUS_SUCCESS", hours_ago)])
    [plan] = data["plans"]
    assert plan["state"] == state
    assert plan["stale"] is (state == "stale")
    assert plan["age_hours"] == round(hours_ago, 2)
    assert data["stale_count"] == (1 if state == "stale" else 0)
    assert data["unscheduled_count"] == (1 if state == "unscheduled" else 0)


async def test_freshness_argument_overrides_schedule(frozen_now):
    ops = [_backup("STATUS_SUCCESS", 3)]
    data, _ = await _freshness(frozen_now, {"cron": "0 2 * * *"}, ops, {"max_age_hours": 2})
    [plan] = data["plans"]
    assert (plan["state"], plan["threshold_hours"], plan["threshold_source"]) == (
        "stale",
        2.0,
        "argument",
    )


async def test_freshness_uses_last_good_backup_and_reports_latest_status(frozen_now):
    ops = [
        _backup("STATUS_ERROR", 1),  # latest backup failed
        _backup("STATUS_SUCCESS", 30, kind="operationIndexSnapshot"),  # not a backup
        _backup("STATUS_WARNING", 5),  # last good one
        _backup("STATUS_SUCCESS", 20),
    ]
    data, route = await _freshness(frozen_now, {"cron": "0 2 * * *"}, ops)
    [plan] = data["plans"]
    assert plan["last_status"] == "STATUS_ERROR"
    assert plan["age_hours"] == 5.0
    assert plan["state"] == "ok"
    assert plan["last_success"].endswith("UTC")
    body = json.loads(route.calls[0].request.content)
    assert body["selector"] == {"planId": "forge-system", "repoGuid": REPO_GUID}
    assert body["lastN"] == 200


async def test_freshness_no_good_backup_is_stale(frozen_now):
    data, _ = await _freshness(frozen_now, {"cron": "0 2 * * *"}, [_backup("STATUS_ERROR", 1)])
    [plan] = data["plans"]
    assert (plan["state"], plan["last_success"], plan["age_hours"]) == ("stale", None, None)


async def test_freshness_no_operations_at_all(frozen_now):
    data, _ = await _freshness(frozen_now, {"maxFrequencyHours": 1}, [])
    [plan] = data["plans"]
    assert (plan["state"], plan["last_status"]) == ("stale", None)


@pytest.mark.parametrize("bad", [0, -1, 10_000])
async def test_freshness_rejects_bad_threshold(mcp_server, bad):
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(f"{BASE_URL}/v1.Backrest/GetConfig")
        result = await mcp_server.call_tool("get_backup_freshness", {"max_age_hours": bad})
    assert "error" in json.loads(result.content[0].text)
    assert route.call_count == 0


# ---------------------------------------------------------------------------
# Every read tool: a backend failure returns the generic error, never exception text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("get_config", {}),
        ("list_plans", {}),
        ("find_plan_for_path", {"path": "/home/ted"}),
        ("get_backup_freshness", {}),
        ("list_snapshots", {}),
        ("list_snapshots", {"repo_id": REPO_ID}),
        ("list_snapshot_files", {"repo_id": REPO_ID, "snapshot_id": "abc"}),
        ("get_summary", {}),
        ("get_operations", {}),
        ("get_logs", {"ref": "abc"}),
        ("get_download_url", {"operation_id": "1", "file_path": "/x"}),
    ],
)
async def test_backend_500_returns_generic_error(mcp_server, tool, args):
    with respx.mock() as mock:
        mock.post(url__startswith=f"{BASE_URL}/v1.Backrest/").mock(
            return_value=httpx.Response(500, text="LEAKCANARY-exception-detail")
        )
        result = await mcp_server.call_tool(tool, args)
    text = result.content[0].text
    assert json.loads(text) == {"error": "tool call failed — check server logs for details"}
    assert "LEAKCANARY" not in text


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("list_snapshots", {"repo_id": "../etc"}),
        ("list_snapshots", {"plan_id": "a b"}),
        ("list_snapshot_files", {"repo_id": "ok", "snapshot_id": "$(x)"}),
        ("get_operations", {"plan_id": "a;b"}),
        ("get_operations", {"repo_id": "a/b"}),
        ("get_download_url", {"operation_id": "1 2", "file_path": "/x"}),
    ],
)
async def test_bad_ids_rejected_before_any_call(mcp_server, tool, args):
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(url__startswith=f"{BASE_URL}/v1.Backrest/")
        result = await mcp_server.call_tool(tool, args)
    assert "error" in json.loads(result.content[0].text)
    assert route.call_count == 0


def test_freshness_helpers(mcp_server):
    import time

    srv = sys.modules["backrest_mcp.server"]
    assert srv._as_int("1760000000000") == 1_760_000_000_000
    assert srv._as_int(None) is None
    assert srv._as_int("x") is None
    assert abs(srv._now() - time.time()) < 5
