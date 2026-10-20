"""Unit tests for the allowlist projection helpers in backrest_mcp.redact (#1176)."""

from __future__ import annotations

import pytest

from backrest_mcp.redact import (
    PLAN_MESSAGES,
    REPO_MESSAGES,
    project_config,
    project_hook,
    project_plan,
    project_repo,
    strip_userinfo,
    unknown_policy_keys,
)


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("rest:https://u:p@h/", "rest:https://h/"),
        ("s3:https://u:p@h/b", "s3:https://h/b"),
        ("https://u@h/", "https://h/"),
        ("rest:https://u:p@h:8000", "rest:https://h:8000"),
        ("rest:https://a@b:c@h/r", "rest:https://h/r"),
        ("rest:https://h/repo", "rest:https://h/repo"),
        ("/mnt/atlas/forge/backups/restic", "/mnt/atlas/forge/backups/restic"),
        ("sftp:u@h:/p", "sftp:u@h:/p"),
        ("rclone:myremote:bucket/path", "rclone:myremote:bucket/path"),
        ("s3:s3.amazonaws.com/bucket", "s3:s3.amazonaws.com/bucket"),
        ("rclone::s3,access_key_id=A,secret_access_key=S:bucket", "rclone:[REDACTED]"),
        ("/mnt/odd=path", "[REDACTED]"),
        ("rest:https://h/repo?token=T", "rest:https://h/repo?[REDACTED]"),
        ("rest:https://u:p@h/repo#frag", "rest:https://h/repo?[REDACTED]"),
        ("https://u:p@[::1]:8000/r", "https://[::1]:8000/r"),
        ("", ""),
    ],
)
def test_strip_userinfo(uri, expected):
    assert strip_userinfo(uri) == expected


@pytest.mark.parametrize(
    "uri",
    [
        "rest:https://u:pa/ss@h/repo",
        # audit F-01: '?' or '#' inside the password must not cut the userinfo short
        "rest:https://user:SECRETa?b@host:8000/repo",
        "rest:https://user:SECRETa#b@host/repo",
        "rest:https://user:SEC/RET?x@host/repo",
        # '@' in the path is indistinguishable from one in a password: over-redact
        "rest:https://u:p@h/a@b",
    ],
)
def test_strip_userinfo_ambiguous_boundary_fails_safe(uri):
    """An unencoded '/', '?' or '#' before the last '@' makes the boundary ambiguous."""
    out = strip_userinfo(uri)
    assert out == uri.split("://", 1)[0] + "://[REDACTED]"
    assert "SECRET" not in out and "SEC" not in out


def test_project_config_tolerates_empty_and_malformed_input():
    assert project_config({}) == {"repos": [], "plans": []}
    assert project_config(None) == {}  # type: ignore[arg-type]
    assert project_config({"repos": ["x", None], "plans": "nope", "auth": "nope"}) == {
        "repos": [],
        "plans": [],
    }


def test_env_entry_without_equals_is_dropped():
    """A bare env entry has no name to show and may be a value."""
    out = project_repo({"id": "r", "env": ["NAME=v", "barevalue-secret"]})
    assert out["envKeys"] == ["NAME"]
    assert "barevalue-secret" not in repr(out)


def test_repo_without_password_reports_false():
    assert project_repo({"id": "r"})["hasPassword"] is False


def test_hook_without_action_has_no_action_key():
    assert project_hook({"conditions": ["CONDITION_ANY_ERROR"]}) == {
        "conditions": ["CONDITION_ANY_ERROR"]
    }


def test_projection_does_not_alias_input():
    """The result is a new structure: mutating it must not touch the raw config."""
    cfg = {"plans": [{"id": "p", "paths": ["/a"]}]}
    out = project_config(cfg)
    out["plans"][0]["paths"].append("/b")
    assert cfg["plans"][0]["paths"] == ["/a"]


# ---------------------------------------------------------------------------
# F-04: nested policy messages are projected through pinned key allowlists
# ---------------------------------------------------------------------------

# Field JSON names from Backrest v1.13.0 proto/v1/config.proto, written out by hand so a
# change to redact.py's allowlists has to change this table too.
_SCHEDULE = {"disabled", "cron", "maxFrequencyDays", "maxFrequencyHours", "clock"}
_RETENTION = {"policyKeepLastN", "policyTimeBucketed", "policyKeepAll"}
_TIME_BUCKETED = {"hourly", "daily", "weekly", "monthly", "yearly", "keepLastN"}
_PINNED = {
    "prunePolicy": {"schedule", "maxUnusedBytes", "maxUnusedPercent"},
    "checkPolicy": {"schedule", "structureOnly", "readDataSubsetPercent"},
    "forgetPolicy": {"schedule", "retention"},
    "commandPrefix": {"ioNice", "cpuNice"},
    "schedule": _SCHEDULE,
    "retention": _RETENTION,
}


def test_nested_message_field_names_are_pinned():
    specs = {**REPO_MESSAGES, **PLAN_MESSAGES}
    assert set(specs) == set(_PINNED)
    for name, fields in _PINNED.items():
        assert set(specs[name]) == fields, name
    for name in ("prunePolicy", "checkPolicy", "forgetPolicy"):
        assert set(REPO_MESSAGES[name]["schedule"]) == _SCHEDULE
    assert set(REPO_MESSAGES["forgetPolicy"]["retention"]) == _RETENTION
    assert set(PLAN_MESSAGES["retention"]["policyTimeBucketed"]) == _TIME_BUCKETED


def _repo_with_unknown_subfields():
    return {
        "id": "r",
        "prunePolicy": {
            "schedule": {"cron": "0 3 * * 0", "futureSched": "LEAKCANARY-a"},
            "maxUnusedPercent": 10,
            "futurePrune": "LEAKCANARY-b",
        },
        "checkPolicy": {"readDataSubsetPercent": 5.0, "futureCheck": {"x": "LEAKCANARY-c"}},
        "forgetPolicy": {
            "schedule": {"maxFrequencyDays": 7},
            "retention": {"policyTimeBucketed": {"daily": 7, "futureBucket": "LEAKCANARY-d"}},
        },
        "commandPrefix": {"ioNice": "IO_IDLE", "futurePrefix": "LEAKCANARY-e"},
    }


def test_unknown_nested_repo_fields_are_dropped():
    out = project_repo(_repo_with_unknown_subfields())
    assert "LEAKCANARY" not in repr(out)
    assert out["prunePolicy"] == {"schedule": {"cron": "0 3 * * 0"}, "maxUnusedPercent": 10}
    assert out["checkPolicy"] == {"readDataSubsetPercent": 5.0}
    assert out["forgetPolicy"] == {
        "schedule": {"maxFrequencyDays": 7},
        "retention": {"policyTimeBucketed": {"daily": 7}},
    }
    assert out["commandPrefix"] == {"ioNice": "IO_IDLE"}


def test_unknown_nested_plan_fields_are_dropped():
    out = project_plan(
        {
            "id": "p",
            "schedule": {"maxFrequencyHours": 6, "clock": "CLOCK_UTC", "x": "LEAKCANARY-f"},
            "retention": {"policyKeepLastN": 30, "y": "LEAKCANARY-g"},
        }
    )
    assert "LEAKCANARY" not in repr(out)
    assert out["schedule"] == {"maxFrequencyHours": 6, "clock": "CLOCK_UTC"}
    assert out["retention"] == {"policyKeepLastN": 30}


def test_non_scalar_in_a_scalar_slot_is_dropped():
    """A known name whose value turned into a message or list is not copied whole."""
    out = project_plan(
        {
            "id": "p",
            "schedule": {"cron": {"expr": "LEAKCANARY-h"}, "clock": ["LEAKCANARY-i"]},
            "retention": "LEAKCANARY-j",
        }
    )
    assert "LEAKCANARY" not in repr(out)
    assert out["schedule"] == {}
    assert out["retention"] == {}


def test_unknown_policy_keys_reports_names_not_values():
    cfg = {"repos": [_repo_with_unknown_subfields()], "plans": [{"schedule": {"z": "LEAKCANARY"}}]}
    found = unknown_policy_keys(cfg)
    assert sorted(found) == [
        "plans[0].schedule.z",
        "repos[0].checkPolicy.futureCheck",
        "repos[0].commandPrefix.futurePrefix",
        "repos[0].forgetPolicy.retention.policyTimeBucketed.futureBucket",
        "repos[0].prunePolicy.futurePrune",
        "repos[0].prunePolicy.schedule.futureSched",
    ]
    assert "LEAKCANARY" not in repr(found)
    assert unknown_policy_keys(None) == []
    assert unknown_policy_keys({"repos": [{"prunePolicy": {"maxUnusedPercent": 1}}]}) == []
