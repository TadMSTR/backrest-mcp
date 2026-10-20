"""Unit tests for scripts/run_live_conformance.py (no live Backrest, no subprocess)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "run_live_conformance.py"
_spec = importlib.util.spec_from_file_location("run_live_conformance", _PATH)
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)


def test_reads_only_backrest_keys(tmp_path):
    env = tmp_path / "forge.env"
    env.write_text(
        "# comment\n"
        "\n"
        "BACKREST_USERNAME=ted\n"
        'export BACKREST_PASSWORD="p=w#1"\n'
        "BACKREST_REPO_PASSWORD='quoted'\n"
        "GITEA_TOKEN=not-for-us\n"
        "BACKREST_NOEQUALS\n"
        "  BACKREST_URL = http://localhost:9898  \n"
    )
    assert runner.read_backrest_env(env) == {
        "BACKREST_USERNAME": "ted",
        "BACKREST_PASSWORD": "p=w#1",
        "BACKREST_REPO_PASSWORD": "quoted",
        "BACKREST_URL": "http://localhost:9898",
    }


def test_child_env_sets_live_flag_and_src_first():
    env = runner.child_env({"PATH": "/bin", "PYTHONPATH": "/other"}, {"BACKREST_USERNAME": "u"})
    assert env["BACKREST_LIVE_TEST"] == "1"
    assert env["BACKREST_URL"] == "http://localhost:9898"
    assert env["BACKREST_USERNAME"] == "u"
    assert env["PYTHONPATH"].split(":") == [str(runner.REPO / "src"), "/other"]


def test_child_env_keeps_configured_url():
    env = runner.child_env({}, {"BACKREST_URL": "http://h:1"})
    assert env["BACKREST_URL"] == "http://h:1"
    assert env["PYTHONPATH"] == str(runner.REPO / "src")


@pytest.mark.parametrize(
    ("rc", "tests", "skipped", "bad", "expected"),
    [
        (0, 10, 1, 0, 0),  # ran, one legitimate skip
        (0, 10, 0, 0, 0),
        (1, 10, 0, 1, 1),  # a failure
        (0, 10, 0, 1, 1),  # junit says failed even if rc did not
        (2, 0, 0, 0, 1),  # pytest itself errored
        (0, 10, 10, 0, 3),  # every test skipped is not a pass
        (0, 0, 0, 0, 3),  # nothing collected
    ],
)
def test_verdict(rc, tests, skipped, bad, expected):
    assert runner.verdict(rc, tests, skipped, bad) == expected


def test_summarise_reads_junit(tmp_path):
    report = tmp_path / "r.xml"
    report.write_text(
        '<testsuites><testsuite tests="9" skipped="2" failures="1" errors="1">'
        "</testsuite></testsuites>"
    )
    assert runner.summarise(report) == (9, 2, 2)
    report.write_text('<testsuite tests="3" skipped="0" failures="0" errors="0"></testsuite>')
    assert runner.summarise(report) == (3, 0, 0)
    report.write_text("<testsuites></testsuites>")
    assert runner.summarise(report) == (0, 0, 0)


def test_missing_env_file_exits_2(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("BACKREST_LIVE_ENV_FILE", str(tmp_path / "absent.env"))
    assert runner.main() == 2
    assert "cannot read env file" in capsys.readouterr().err
