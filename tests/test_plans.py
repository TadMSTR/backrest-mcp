"""Table-driven tests for the pure plan helpers in backrest_mcp.plans."""

from __future__ import annotations

import fnmatch
import random
import time

import pytest

from backrest_mcp.plans import (
    CRON_STALE_HOURS,
    MAX_PATH_COMPONENTS,
    MAX_PATH_LENGTH,
    covering_root,
    excluded_by,
    normalise_path,
    pattern_matches,
    stale_threshold,
)


@pytest.mark.parametrize(
    ("pattern", "path", "ignore_case", "expected"),
    [
        # unanchored basename: matches at any depth
        ("*.tmp", "/a/b/x.tmp", False, True),
        ("*.tmp", "/a/b/x.tmpl", False, False),
        (".cache", "/home/ted/.cache/x", False, True),
        ("node_modules", "/srv/app/node_modules/pkg/index.js", False, True),
        ("ca?he", "/x/cache", False, True),
        ("[ab].txt", "/x/b.txt", False, True),
        ("[ab].txt", "/x/c.txt", False, False),
        # anchored: only from the root, and an excluded directory excludes its contents
        ("/home/ted/.cache", "/home/ted/.cache", False, True),
        ("/home/ted/.cache", "/home/ted/.cache/foo/bar", False, True),
        ("/home/ted/.cache", "/other/home/ted/.cache", False, False),
        ("/home/ted/.cache", "/home/ted/.cachex", False, False),
        # * stays inside one component; ** spans any number, including none
        ("/home/*/Downloads", "/home/ted/Downloads/a", False, True),
        ("/home/*/Downloads", "/home/ted/x/Downloads", False, False),
        ("/home/**/Downloads", "/home/ted/x/Downloads/f", False, True),
        ("/home/**/Downloads", "/home/Downloads", False, True),
        ("**/*.iso", "/a/b/c.iso", False, True),
        ("/var/**", "/var", False, True),
        # case
        ("*.JPG", "/p/a.jpg", False, False),
        ("*.JPG", "/p/a.jpg", True, True),
        ("/Home/Ted", "/home/ted/x", True, True),
        # not modelled: negation, empty
        ("!keep", "/x/keep", False, False),
        ("", "/x", False, False),
    ],
)
def test_pattern_matches(pattern, path, ignore_case, expected):
    assert pattern_matches(pattern, path, ignore_case=ignore_case) is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/", "/"),
        ("/a/../b", "/b"),
        ("//etc/", "/etc"),
        ("/home/ted/./Documents/", "/home/ted/Documents"),
        ("/" + "a" * (MAX_PATH_LENGTH - 1), "/" + "a" * (MAX_PATH_LENGTH - 1)),
    ],
)
def test_normalise_path(raw, expected):
    assert normalise_path(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "relative/path",
        "~/x",
        None,
        5,
        "/a\x00b",
        "/" + "a" * MAX_PATH_LENGTH,
        "/a" * (MAX_PATH_COMPONENTS + 1),
    ],
)
def test_normalise_path_rejects(raw):
    with pytest.raises(ValueError):
        normalise_path(raw)


@pytest.mark.parametrize(
    ("paths", "path", "expected"),
    [
        (["/home/ted"], "/home/ted", "/home/ted"),
        (["/home/ted"], "/home/ted/x/y", "/home/ted"),
        (["/home/ted"], "/home/tedx", None),
        (["/home/ted/"], "/home/ted/x", "/home/ted/"),
        (["/"], "/anything/at/all", "/"),
        (["relative", "/etc"], "/etc/hosts", "/etc"),
        (["/a", "/a/b"], "/a/b/c", "/a"),
        ([], "/x", None),
        ("not-a-list", "/x", None),
    ],
)
def test_covering_root(paths, path, expected):
    assert covering_root({"paths": paths}, path) == expected


def test_excluded_by_checks_excludes_then_iexcludes():
    plan = {"excludes": ["*.tmp", "/home/ted/.cache"], "iexcludes": ["*.ISO"]}
    assert excluded_by(plan, "/home/ted/a.tmp") == "*.tmp"
    assert excluded_by(plan, "/home/ted/.cache/x") == "/home/ted/.cache"
    assert excluded_by(plan, "/home/ted/disk.iso") == "*.ISO"
    assert excluded_by(plan, "/home/ted/notes.md") is None
    assert excluded_by({}, "/x") is None


@pytest.mark.parametrize(
    ("schedule", "max_age", "expected"),
    [
        (None, None, (None, "unscheduled")),
        ({}, None, (None, "unscheduled")),
        ({"clock": "CLOCK_LOCAL"}, None, (None, "unscheduled")),
        ({"disabled": True}, None, (None, "unscheduled")),
        ({"disabled": True}, 5.0, (None, "unscheduled")),
        ({"maxFrequencyHours": 6}, None, (12.0, "2x maxFrequencyHours")),
        ({"maxFrequencyDays": 1}, None, (48.0, "2x maxFrequencyDays")),
        ({"maxFrequencyHours": 0, "cron": "0 2 * * *"}, None, (CRON_STALE_HOURS, "cron default")),
        ({"cron": "0 2 * * *", "clock": "CLOCK_UTC"}, None, (26.0, "cron default")),
        ({"cron": "0 2 * * *"}, 3, (3.0, "argument")),
        ({"maxFrequencyDays": 7}, 1.5, (1.5, "argument")),
    ],
)
def test_stale_threshold(schedule, max_age, expected):
    assert stale_threshold(schedule, max_age) == expected


def test_component_cap_boundary():
    assert normalise_path("/a" * MAX_PATH_COMPONENTS) == "/a" * MAX_PATH_COMPONENTS


@pytest.mark.parametrize("pattern", ["**/node_modules", "node_modules", "/**/**/**/x", "*/*/*/x"])
def test_matching_is_fast_at_the_component_cap(pattern):
    """Pre-audit IV-47: the recursive matcher took ~3.4 s for `**/x` at 400 components."""
    path = "/a" * MAX_PATH_COMPONENTS
    t0 = time.perf_counter()
    for _ in range(10):
        pattern_matches(pattern, path)
    assert time.perf_counter() - t0 < 1.0


def _reference(pattern, path, ignore_case=False):
    """The original recursive matcher, kept as an oracle for the linear one."""

    def comps(p):
        return [c for c in p.split("/") if c]

    def match(pat, parts):
        if not pat:
            return not parts
        head, rest = pat[0], pat[1:]
        if head == "**":
            return any(match(rest, parts[i:]) for i in range(len(parts) + 1))
        return bool(parts) and fnmatch.fnmatchcase(parts[0], head) and match(rest, parts[1:])

    if not pattern or pattern.startswith("!"):
        return False
    if ignore_case:
        pattern, path = pattern.lower(), path.lower()
    pat = comps(pattern)
    if not pattern.startswith("/"):
        pat = ["**", *pat]
    parts = comps(path)
    return any(match(pat, parts[:n]) for n in range(1, len(parts) + 1))


def test_linear_matcher_agrees_with_the_recursive_one():
    rng = random.Random(1182)
    atoms = ["a", "b", "ab", "A", "*", "?", "**", "[ab]", "a*", "*b"]
    names = ["a", "b", "ab", "A", "ba", "abc"]
    for _ in range(4000):
        pattern = "/".join(rng.choice(atoms) for _ in range(rng.randint(1, 4)))
        if rng.random() < 0.5:
            pattern = "/" + pattern
        path = "/" + "/".join(rng.choice(names) for _ in range(rng.randint(1, 6)))
        ic = rng.random() < 0.3
        assert pattern_matches(pattern, path, ignore_case=ic) == _reference(pattern, path, ic), (
            pattern,
            path,
            ic,
        )
