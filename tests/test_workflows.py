"""
Workflow invariants that nothing else in CI enforces (B12, F6, F7).

repo-conform checks these, but it runs on forge, not on a pull request. A tag-pinned
`uses:` or a workflow without a top-level `permissions:` block would otherwise merge green.
tests/check_action_pins.py covers the other half (one commit per action).

Each check is also run against a planted violation, so a pattern that stopped matching
cannot pass the real tree by matching nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"

# Every `uses:` line, local (`./…`) references excluded: they have no `@` to pin.
_USES = re.compile(r"^[ \t]*(?:-[ \t]+)?uses:[ \t]*['\"]?(?P<spec>[^\s'\"#]+)", re.MULTILINE)
_PINNED = re.compile(r"^[^@\s]+@[0-9a-f]{40}['\"]?[ \t]+#[ \t]*v\d+(\.\d+)*[ \t]*$")
_TOP_PERMISSIONS = re.compile(r"^permissions:", re.MULTILINE)


def _workflow_files() -> list[Path]:
    return sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))


def unpinned(text: str) -> list[str]:
    """`uses:` lines that are not `owner/repo@<40-hex> # vX.Y.Z`."""
    bad = []
    for line in text.splitlines():
        m = _USES.match(line)
        if not m or m["spec"].startswith("./"):
            continue
        rest = line.split("uses:", 1)[1].strip().lstrip("'\"")
        if not _PINNED.match(rest):
            bad.append(rest)
    return bad


def test_workflows_exist():
    assert len(_workflow_files()) >= 5


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_every_action_is_sha_pinned_with_version_comment(path):
    text = path.read_text()
    assert _USES.search(text), f"{path.name}: no uses: lines matched"
    assert unpinned(text) == []


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_every_workflow_has_top_level_permissions(path):
    assert _TOP_PERMISSIONS.search(path.read_text())


def test_pin_check_fires_on_planted_violations():
    sha = "3d3c42e5aac5ba805825da76410c181273ba90b1"
    planted = (
        "steps:\n"
        "      - uses: actions/checkout@v4\n"
        f"      - uses: actions/checkout@{sha}\n"
        f"      - uses: 'actions/checkout@{sha}'  # not-a-version\n"
        f"      - uses: actions/checkout@{sha} # v7.0.1\n"
        "      - uses: ./.github/actions/local\n"
    )
    assert len(unpinned(planted)) == 3


def test_permissions_check_fires_on_planted_violation():
    assert not _TOP_PERMISSIONS.search("jobs:\n  x:\n    permissions:\n      contents: read\n")


def test_setup_uv_pins_the_uv_version():
    """A SHA-pinned setup-uv still installs the latest uv unless `version:` is set (F-06)."""
    text = (WORKFLOWS / "ci.yml").read_text()
    uses = text.count("uses: astral-sh/setup-uv@")
    pinned = len(
        re.findall(
            r'uses: astral-sh/setup-uv@\S+.*\n\s+with:\n(?:\s+#.*\n)*\s+version: "\d+\.\d+\.\d+"',
            text,
        )
    )
    assert uses >= 1
    assert pinned == uses
