#!/usr/bin/env python3
"""Run tests/test_live_conformance.py against the live Backrest, with credentials kept off argv.

Reads every ``BACKREST_*`` variable from the env file named by ``BACKREST_LIVE_ENV_FILE``
(default ``~/.secrets/forge.env``) inside this process and hands them to pytest through the
child's environment. Nothing secret is ever on a command line, in this script's output, or
in pytest's (the live tests assert on bools with fixed messages, and run with
``--tb=line``).

The child imports the package from this tree's ``src/`` (prepended to PYTHONPATH), so the
production venv's interpreter can test a worktree without reinstalling anything:

    /home/ted/repos/personal/backrest-mcp/.venv/bin/python scripts/run_live_conformance.py

Exit status: 0 when the live tests ran and passed; 1 on a failure or error; 2 when the env
file is missing or unreadable; 3 when every test skipped, which is not a pass (a typo in
BACKREST_LIVE_TEST or a missing credential looks exactly like that).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LIVE_FILE = "tests/test_live_conformance.py"
DEFAULT_ENV_FILE = "~/.secrets/forge.env"


def read_backrest_env(path: Path) -> dict[str, str]:
    """``BACKREST_*`` assignments from a dotenv-style file. Other keys are ignored."""
    found: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").strip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key.startswith("BACKREST_"):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        found[key] = value
    return found


def child_env(base: dict[str, str], creds: dict[str, str]) -> dict[str, str]:
    env = {**base, **creds, "BACKREST_LIVE_TEST": "1"}
    env.setdefault("BACKREST_URL", "http://localhost:9898")
    src = str(REPO / "src")
    env["PYTHONPATH"] = os.pathsep.join(p for p in (src, base.get("PYTHONPATH", "")) if p)
    return env


def summarise(junit_xml: Path) -> tuple[int, int, int]:
    """(tests, skipped, failed_or_errored) from a pytest junit report."""
    root = ET.parse(junit_xml).getroot()
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    if suite is None:
        return 0, 0, 0
    tests = int(suite.get("tests", 0))
    skipped = int(suite.get("skipped", 0))
    bad = int(suite.get("failures", 0)) + int(suite.get("errors", 0))
    return tests, skipped, bad


def verdict(returncode: int, tests: int, skipped: int, bad: int) -> int:
    if returncode != 0 or bad:
        return 1
    if tests == 0 or skipped >= tests:
        return 3
    return 0


def main() -> int:
    env_file = Path(os.path.expanduser(os.environ.get("BACKREST_LIVE_ENV_FILE", DEFAULT_ENV_FILE)))
    try:
        creds = read_backrest_env(env_file)
    except OSError as e:
        print(f"cannot read env file {env_file}: {e.strerror}", file=sys.stderr)
        return 2
    print(f"env file: {env_file} ({len(creds)} BACKREST_* keys: {', '.join(sorted(creds))})")

    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "live.xml"
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-rs",
            "-p",
            "no:cacheprovider",
            "--tb=line",
            f"--junitxml={report}",
            LIVE_FILE,
        ]
        proc = subprocess.run(cmd, cwd=REPO, env=child_env(dict(os.environ), creds), check=False)
        tests, skipped, bad = summarise(report) if report.exists() else (0, 0, 0)

    code = verdict(proc.returncode, tests, skipped, bad)
    print(
        f"live conformance: {tests - skipped} ran, {skipped} skipped, {bad} failed -> exit {code}"
    )
    return code


if __name__ == "__main__":
    sys.exit(main())
