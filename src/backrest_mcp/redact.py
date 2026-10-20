"""
Allowlist projection of the Backrest config (#1176).

Backrest's GetConfig returns ``repos[].password`` (the restic key) in cleartext, along
with ``env`` values, credential-bearing ``uri`` userinfo, restic ``flags``, hook targets
(webhook URLs, bot tokens, commands) and the multihost identity. Its own sanitiser masks
only ``auth.users[].passwordBcrypt``.

Everything here builds a **new** dict from named keys only. A key that is not listed is
dropped by construction, so a secret field added by a future Backrest version never reaches
a caller. Do not add a "pass through anything else" branch.

Pure functions, no I/O: other tools (list_plans, find_plan_for_path) reuse them.

Field names follow Backrest v1.13.0 ``proto/v1/config.proto``.
"""

from __future__ import annotations

import copy
from typing import Any

_TOP_LEVEL_KEYS = ("modno", "version", "instance")

_REPO_KEYS = (
    "id",
    "guid",
    "autoUnlock",
    "autoInitialize",
    "shared",
    "originInstanceId",
)

_PLAN_KEYS = (
    "id",
    "repo",
    "paths",
    "excludes",
    "iexcludes",
    "skipIfUnchanged",
)

# Nested policy messages, projected through their own key allowlists (audit F-04). A field
# Backrest adds to one of them is dropped until it is named here, the same rule as the top
# level. ``None`` marks a scalar or enum (only str/int/float/bool pass); a dict marks a
# nested message. Names are the JSON names in v1.13.0 config.proto.
_SCHEDULE: dict = {
    "disabled": None,
    "cron": None,
    "maxFrequencyDays": None,
    "maxFrequencyHours": None,
    "clock": None,
}
_RETENTION: dict = {
    "policyKeepLastN": None,
    "policyTimeBucketed": {
        "hourly": None,
        "daily": None,
        "weekly": None,
        "monthly": None,
        "yearly": None,
        "keepLastN": None,
    },
    "policyKeepAll": None,
}
REPO_MESSAGES: dict[str, dict] = {
    "prunePolicy": {"schedule": _SCHEDULE, "maxUnusedBytes": None, "maxUnusedPercent": None},
    "checkPolicy": {"schedule": _SCHEDULE, "structureOnly": None, "readDataSubsetPercent": None},
    "forgetPolicy": {"schedule": _SCHEDULE, "retention": _RETENTION},
    "commandPrefix": {"ioNice": None, "cpuNice": None},
}
PLAN_MESSAGES: dict[str, dict] = {
    "schedule": _SCHEDULE,
    "retention": _RETENTION,
}

_SCALARS = (str, int, float, bool)

_HOOK_KEYS = ("conditions", "onError")

REDACTED = "[REDACTED]"


def _pick(src: dict, keys: tuple[str, ...]) -> dict:
    return {k: copy.deepcopy(src[k]) for k in keys if k in src}


def _project_message(value: Any, spec: dict) -> dict:
    """Copy only the keys ``spec`` names, recursing into nested messages."""
    if not isinstance(value, dict):
        return {}
    out: dict = {}
    for key, sub in spec.items():
        if key not in value:
            continue
        v = value[key]
        if sub is None:
            if isinstance(v, _SCALARS):
                out[key] = v
        elif isinstance(v, dict):
            out[key] = _project_message(v, sub)
    return out


def _pick_messages(src: dict, messages: dict[str, dict]) -> dict:
    return {k: _project_message(src[k], spec) for k, spec in messages.items() if k in src}


def _unknown(value: Any, spec: dict, where: str) -> list[str]:
    if not isinstance(value, dict):
        return []
    found = []
    for key, v in value.items():
        if key not in spec:
            found.append(f"{where}.{key}")
        elif isinstance(spec[key], dict):
            found.extend(_unknown(v, spec[key], f"{where}.{key}"))
    return found


def unknown_policy_keys(cfg: Any) -> list[str]:
    """Key paths inside the nested policy messages that the allowlists do not name.

    Returns names only, for example ``repos[0].prunePolicy.newField``, never values. The
    live conformance check uses it to report a new Backrest field before anyone needs it.
    """
    if not isinstance(cfg, dict):
        return []
    found: list[str] = []
    for kind, messages in (("repos", REPO_MESSAGES), ("plans", PLAN_MESSAGES)):
        for i, item in enumerate(_dicts(cfg.get(kind))):
            for name, spec in messages.items():
                found.extend(_unknown(item.get(name), spec, f"{kind}[{i}].{name}"))
    return found


def _dicts(value: Any) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, dict)]


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str)]


def strip_userinfo(uri: str) -> str:
    """Remove ``user[:pass]@`` from a URL-shaped repo URI.

    Handles plain URLs (``https://u:p@h/``) and restic's ``backend:scheme://`` forms
    (``rest:https://u:p@h/repo``, ``s3:https://u:p@h/bucket``) by string handling, since
    ``urllib.parse.urlsplit`` does not parse ``rest:https://…`` as a URL.

    Userinfo is decided on everything after ``://``, before anything else is cut. If that
    text holds an ``@``, the part before the last ``@`` must be a clean userinfo, with no
    ``/``, ``?`` or ``#``. Then it is dropped. Otherwise the boundary is ambiguous: for
    example a password with an unencoded ``/``, ``?`` or ``#``, or an ``@`` in the path. In
    that case everything after ``://`` is replaced with ``[REDACTED]``. This over-redacts a
    URI with ``@`` in its path rather than risk returning part of a password.

    After userinfo is removed, a query string or fragment is replaced with ``?[REDACTED]``,
    since tokens are sometimes passed there.

    A URI without ``://`` that contains ``=`` is redacted after its backend prefix. This
    catches restic's ``rclone:`` backend with an inline rclone connection string
    (``rclone::s3,access_key_id=…,secret_access_key=…:bucket``), which carries credentials
    and has no ``://``.

    Other URIs without ``://`` are returned unchanged: local paths (``/mnt/…``) and restic's
    short ``sftp:user@host:/path`` form. In the short sftp form, ``user`` is an SSH login
    name, not a secret. SFTP cannot carry a password in the URI; authentication is by SSH
    key or agent.
    """
    if not uri:
        return uri
    if "://" not in uri:
        if "=" not in uri:
            return uri
        head, sep, _ = uri.partition(":")
        return f"{head}:{REDACTED}" if sep and head and "/" not in head else REDACTED
    prefix, rest = uri.split("://", 1)
    prefix += "://"
    # Decide userinfo on the whole remainder first: cutting at "?" or "#" before looking for
    # "@" would cut a password that contains one and return its prefix (audit F-01).
    if "@" in rest:
        userinfo, rest = rest.rsplit("@", 1)
        if any(c in userinfo for c in "/?#"):
            return prefix + REDACTED
    for mark in ("?", "#"):
        if mark in rest:
            rest = rest.split(mark, 1)[0] + "?" + REDACTED
    return prefix + rest


def project_hook(hook: dict) -> dict:
    """Project a repo or plan hook to its conditions, onError and action *name*.

    ``action`` is the name of the oneof key that is set (e.g. ``"actionHealthchecks"``).
    No field of the action body is returned: it holds commands, webhook URLs (with the
    token in the URL), Gotify tokens, Shoutrrr URLs and Telegram bot tokens.
    """
    out = _pick(hook, _HOOK_KEYS)
    action = next((k for k in hook if isinstance(k, str) and k.startswith("action")), None)
    if action is not None:
        out["action"] = action
    return out


def project_repo(repo: dict) -> dict:
    """Project one repo. Never returns ``password``, ``env`` values or ``flags``."""
    out = _pick(repo, _REPO_KEYS)
    out.update(_pick_messages(repo, REPO_MESSAGES))
    uri = repo.get("uri")
    if isinstance(uri, str):
        out["uri"] = strip_userinfo(uri)
    # Names only. An entry with no "=" has no name to show, and may be a bare value.
    out["envKeys"] = [e.split("=", 1)[0] for e in _strings(repo.get("env")) if "=" in e]
    out["hasPassword"] = bool(repo.get("password"))
    out["flagCount"] = len(_strings(repo.get("flags")))
    out["hooks"] = [project_hook(h) for h in _dicts(repo.get("hooks"))]
    return out


def project_plan(plan: dict) -> dict:
    """Project one plan. ``backup_flags`` is reduced to a count."""
    out = _pick(plan, _PLAN_KEYS)
    out.update(_pick_messages(plan, PLAN_MESSAGES))
    out["backupFlagCount"] = len(_strings(plan.get("backup_flags")))
    out["hooks"] = [project_hook(h) for h in _dicts(plan.get("hooks"))]
    return out


def project_auth(auth: dict) -> dict:
    """Project auth to ``disabled`` and user names. ``passwordBcrypt`` is never returned."""
    out = _pick(auth, ("disabled",))
    out["users"] = [_pick(u, ("name",)) for u in _dicts(auth.get("users"))]
    return out


def project_config(cfg: dict) -> dict:
    """Build a secret-free projection of a Backrest GetConfig response.

    Returns ``modno``, ``version``, ``instance``, projected ``repos`` and ``plans``, and
    ``auth`` reduced to ``disabled`` plus user names. The multihost ``sync`` block
    (private identity, pairing secrets) is omitted entirely, as is any key not named here.
    """
    if not isinstance(cfg, dict):
        return {}
    out = _pick(cfg, _TOP_LEVEL_KEYS)
    out["repos"] = [project_repo(r) for r in _dicts(cfg.get("repos"))]
    out["plans"] = [project_plan(p) for p in _dicts(cfg.get("plans"))]
    if isinstance(cfg.get("auth"), dict):
        out["auth"] = project_auth(cfg["auth"])
    return out
