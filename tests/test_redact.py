"""Unit tests for the allowlist projection helpers in backrest_mcp.redact (#1176)."""

from __future__ import annotations

import pytest

from backrest_mcp.redact import project_config, project_hook, project_repo, strip_userinfo


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
        ("", ""),
    ],
)
def test_strip_userinfo(uri, expected):
    assert strip_userinfo(uri) == expected


def test_strip_userinfo_ambiguous_boundary_fails_safe():
    """An unencoded '/' in the password hides the '@' from the authority: redact it all."""
    assert strip_userinfo("rest:https://u:pa/ss@h/repo") == "rest:https://[REDACTED]"


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
