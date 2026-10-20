"""
Pure helpers for the plan tools: path coverage and backup freshness.

They work on the *projected* plans from ``redact.project_plan``, never on the raw config,
so nothing here can return a secret that the projection dropped. No I/O: path matching is
string matching and never touches the filesystem.

Exclude matching approximates restic's ``--exclude`` / ``--iexclude`` semantics:

- a pattern that does not start with ``/`` matches at any depth (restic prefixes ``**/``)
- ``*``, ``?`` and ``[...]`` match within one path component; ``**`` matches any number of
  components, including none
- a pattern that matches a directory excludes everything under it
- ``iexcludes`` compare case-insensitively

Not modelled: ``!`` negation, environment variables in patterns, ``--exclude-file``,
``--exclude-if-present``, ``--exclude-larger-than`` and anything passed in backup flags.
Responses therefore say ``"match": "approximate"``.
"""

from __future__ import annotations

import fnmatch
import posixpath
from typing import Any

MAX_PATH_LENGTH = 4096

# Default staleness threshold for cron schedules, which this module does not parse.
CRON_STALE_HOURS = 26.0


def normalise_path(path: Any) -> str:
    """Validate and normalise an absolute path. Raises ValueError on anything else."""
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError("path must be an absolute path starting with '/'")
    if len(path) > MAX_PATH_LENGTH:
        raise ValueError(f"path longer than {MAX_PATH_LENGTH} characters")
    if "\x00" in path:
        raise ValueError("path contains a NUL byte")
    # normpath keeps a leading "//"; collapse it so "//etc" and "/etc" compare equal.
    return "/" + posixpath.normpath(path).lstrip("/")


def _components(path: str) -> list[str]:
    return [c for c in path.split("/") if c]


def _match_parts(pat: list[str], parts: list[str]) -> bool:
    if not pat:
        return not parts
    head, rest = pat[0], pat[1:]
    if head == "**":
        return any(_match_parts(rest, parts[i:]) for i in range(len(parts) + 1))
    return bool(parts) and fnmatch.fnmatchcase(parts[0], head) and _match_parts(rest, parts[1:])


def pattern_matches(pattern: str, path: str, *, ignore_case: bool = False) -> bool:
    """True if ``pattern`` excludes ``path`` (or one of its ancestors)."""
    if not pattern or pattern.startswith("!"):
        return False
    if ignore_case:
        pattern, path = pattern.lower(), path.lower()
    pat = _components(pattern)
    if not pattern.startswith("/"):
        pat = ["**", *pat]
    parts = _components(path)
    # An excluded directory excludes its contents: try the path and every ancestor.
    return any(_match_parts(pat, parts[:n]) for n in range(1, len(parts) + 1))


def _strings(value: Any) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def covering_root(plan: dict, path: str) -> str | None:
    """The first of the plan's ``paths`` that is ``path`` or an ancestor of it."""
    for root in _strings(plan.get("paths")):
        if not root.startswith("/"):
            continue
        norm = "/" + posixpath.normpath(root).lstrip("/")
        if norm == "/" or path == norm or path.startswith(norm + "/"):
            return root
    return None


def excluded_by(plan: dict, path: str) -> str | None:
    """The first ``excludes`` or ``iexcludes`` pattern that matches, else None."""
    for pattern in _strings(plan.get("excludes")):
        if pattern_matches(pattern, path):
            return pattern
    for pattern in _strings(plan.get("iexcludes")):
        if pattern_matches(pattern, path, ignore_case=True):
            return pattern
    return None


def stale_threshold(schedule: Any, max_age_hours: float | None) -> tuple[float | None, str]:
    """Return ``(threshold_hours, source)``. ``None`` hours means the plan is unscheduled.

    A disabled or missing schedule is unscheduled even when the caller gives a threshold:
    a plan nobody runs is not stale, it is off.
    """
    if not isinstance(schedule, dict) or not schedule or schedule.get("disabled"):
        return None, "unscheduled"
    if max_age_hours is not None:
        return float(max_age_hours), "argument"
    hours = schedule.get("maxFrequencyHours")
    if isinstance(hours, int | float) and hours > 0:
        return 2.0 * hours, "2x maxFrequencyHours"
    days = schedule.get("maxFrequencyDays")
    if isinstance(days, int | float) and days > 0:
        return 2.0 * 24 * days, "2x maxFrequencyDays"
    if schedule.get("cron"):
        return CRON_STALE_HOURS, "cron default"
    return None, "unscheduled"
