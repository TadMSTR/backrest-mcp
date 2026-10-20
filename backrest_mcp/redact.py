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

# Policy/schedule messages hold only scalars and enums in v1.13.0 (PrunePolicy, CheckPolicy,
# ForgetPolicy, CommandPrefix, Schedule, RetentionPolicy), so they are copied whole.
_REPO_KEYS = (
    "id",
    "guid",
    "autoUnlock",
    "autoInitialize",
    "shared",
    "originInstanceId",
    "prunePolicy",
    "checkPolicy",
    "forgetPolicy",
    "commandPrefix",
)

_PLAN_KEYS = (
    "id",
    "repo",
    "paths",
    "excludes",
    "iexcludes",
    "schedule",
    "retention",
    "skipIfUnchanged",
)

_HOOK_KEYS = ("conditions", "onError")

REDACTED = "[REDACTED]"


def _pick(src: dict, keys: tuple[str, ...]) -> dict:
    return {k: copy.deepcopy(src[k]) for k in keys if k in src}


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

    The authority is everything between ``://`` and the next ``/``. If it holds an ``@``,
    the part up to its last ``@`` is dropped. If the authority has no ``@`` but the rest of
    the URI does (for example a password containing an unencoded ``/``), the boundary is
    ambiguous, so everything after ``://`` is replaced with ``[REDACTED]``. This fails safe.

    A query string or fragment on a URL-shaped URI is replaced with ``?[REDACTED]``, since
    tokens are sometimes passed there.

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
    for mark in ("?", "#"):
        if mark in rest:
            rest = rest.split(mark, 1)[0] + "?" + REDACTED
    slash = rest.find("/")
    authority, path = (rest, "") if slash == -1 else (rest[:slash], rest[slash:])
    if "@" in authority:
        return prefix + authority.rsplit("@", 1)[1] + path
    if "@" in path:
        return prefix + REDACTED
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
