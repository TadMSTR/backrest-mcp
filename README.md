# backrest-mcp

[![Built with Claude Code](https://img.shields.io/badge/Built_with-Claude_Code-6B57FF?logo=claude&logoColor=white)](https://claude.ai/code)
[![CI](https://github.com/TadMSTR/backrest-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/TadMSTR/backrest-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

MCP server for [Backrest](https://github.com/garethgeorge/backrest) — a web UI and orchestrator for restic backups.

Python/FastMCP rewrite of `backrest-mcp-server`. Covers the full useful surface of the Backrest REST API with layered safety controls to protect backup data.

## Tools

| Tool | Description | Key params | Returns | Requires |
|------|-------------|------------|---------|----------|
| `get_health` | Check reachability + credential health (poll-safe) | none | `status` (`ok` / `auth_failed` / `unreachable`), `backrest_url`; when ok, repo and plan counts and `config_version` | none |
| `get_config` | Read a secret-free projection of the Backrest configuration (see [get_config output](#get_config-output)) | none | `modno`, `version`, `instance`, `repos`, `plans`, `auth` | none |
| `list_plans` | Each plan's paths, excludes, repo, schedule and retention (secret-free) | none | `plans` (same projection as `get_config`), `count` | none |
| `find_plan_for_path` | Which plans cover an absolute path, and which exclude pattern drops it (approximate restic matching) | `path` (absolute; never read from disk) | `path`, `match: "approximate"`, `backed_up`, `plans[]` with `plan_id`, `repo`, `covered_by`, `excluded_by`, `schedule` | none |
| `get_backup_freshness` | Per plan: last good backup, age, latest status, stale / ok / unscheduled | `max_age_hours` (optional, > 0 and <= 8784) | `plans[]` (`plan_id`, `repo`, `last_success`, `age_hours`, `last_status`, `threshold_hours`, `threshold_source`, `state`, `stale`), `stale_count`, `unscheduled_count`, `checked_at` | none |
| `list_snapshots` | List snapshots; no args → all repos merged | `repo_id`, `plan_id` (both optional) | `snapshots[]` (ID, time, hostname, paths, tags, `repoId`) | none |
| `list_snapshot_files` | Browse files within a snapshot | `repo_id`, `snapshot_id`, `path` (default `/`) | `path`, `entries[]` | none |
| `get_summary` | 30-day dashboard stats per repo and plan | none | Per-repo and per-plan backup counts, bytes, snapshot totals, next backup time | none |
| `get_operations` | Recent operation history with status icons + log refs | `plan_id`, `repo_id` (optional), `limit` (default 20) | `operations` (formatted lines), `count` | none |
| `get_logs` | Read an operation's log output | `ref` (from `get_operations`), `max_bytes` (default 100000; tail kept) | `ref`, `bytes`, `truncated`, `log` | none |
| `get_download_url` | Signed download URL for a restored file | `operation_id`, `file_path` | `url`, `operation_id`, `file_path` | none |
| `trigger_backup` | Trigger a backup plan | `plan_id`, `dry_run` (default false) | Backrest's response | `BACKREST_READONLY=false` |
| `do_repo_task` | Run maintenance on a repo | `repo_id`, `task` (`index` / `prune` / `check` / `stats` / `unlock` / `forget`) | Backrest's response | `BACKREST_READONLY=false` |
| `cancel_operation` | Cancel a running operation | `operation_id` | Backrest's response | `BACKREST_READONLY=false` |
| `forget_snapshot` | Permanently forget a snapshot | `snapshot_id`, `repo_id`, `confirm` (`FORGET:<snapshot_id>`), `plan_id` | Backrest's response, or an error on a wrong token | `BACKREST_ALLOW_DESTRUCTIVE=true` |
| `restore_snapshot` | Restore a snapshot to a staging path | `snapshot_id`, `repo_id`, `path` (in snapshot), `target` (under the restore prefix), `plan_id` | Backrest's response, or an error on a refused prefix or target | `BACKREST_ALLOW_DESTRUCTIVE=true` |

A failed call returns `{"error": "tool call failed — check server logs for details"}`
rather than raising; the detail goes to the server log only. ID arguments (`repo_id`,
`plan_id`, `snapshot_id`, `operation_id`, `ref`) must match `^[a-zA-Z0-9_.-]{1,256}$` and
are checked before Backrest is called.

Default state: **read-only** — only the 11 read tools are registered. No write calls are
possible without explicit opt-in. `RunCommand`, `SetConfig`, `AddRepo`, `RemoveRepo`, and
`ClearHistory` are intentionally never exposed.

For "is this path backed up?" use `find_plan_for_path`; for "is anything stale?" use
`get_backup_freshness`. Both read the same secret-free projection as `get_config`, so they
never see a repo password.

> API field names target the **deployed Backrest v1.13.0** connect-rpc API, which upstream
> `main` has since diverged from. See `CHANGELOG.md` (0.3.0) for the reconciliation notes.

## get_config output

Backrest's `GetConfig` returns the restic repository password, env values and hook tokens
in cleartext. `get_config` does not pass that response through. It builds a new object from
named keys only (`src/backrest_mcp/redact.py`), so any field not listed below is dropped,
including a field a future Backrest version adds. `list_plans`, `find_plan_for_path` and
`get_backup_freshness` read the same projection.

| Level | Fields returned |
|-------|-----------------|
| Top level | `modno`, `version`, `instance`, `repos`, `plans`, `auth` |
| `repos[]` | `id`, `guid`, `uri` (redacted, see below), `autoUnlock`, `autoInitialize`, `shared`, `originInstanceId`, `prunePolicy`, `checkPolicy`, `forgetPolicy`, `commandPrefix`, `envKeys` (variable names only), `hasPassword` (bool), `flagCount` (int), `hooks` |
| `plans[]` | `id`, `repo`, `paths`, `excludes`, `iexcludes`, `schedule`, `retention`, `skipIfUnchanged`, `backupFlagCount` (int), `hooks` |
| `hooks[]` (repo and plan) | `conditions`, `onError`, `action` (the action type name only, e.g. `actionHealthchecks`) |
| `auth` | `disabled`, `users[].name` |

The nested policy messages have their own allowlists of v1.13.0 field names, so they are
not copied whole either:

| Message | Fields returned |
|---------|-----------------|
| `schedule` (plans, and inside each repo policy) | `disabled`, `cron`, `maxFrequencyDays`, `maxFrequencyHours`, `clock` |
| `retention` (plans, and `forgetPolicy.retention`) | `policyKeepLastN`, `policyKeepAll`, `policyTimeBucketed` (`hourly`, `daily`, `weekly`, `monthly`, `yearly`, `keepLastN`) |
| `prunePolicy` | `schedule`, `maxUnusedBytes`, `maxUnusedPercent` |
| `checkPolicy` | `schedule`, `structureOnly`, `readDataSubsetPercent` |
| `forgetPolicy` | `schedule`, `retention` |
| `commandPrefix` | `ioNice`, `cpuNice` |

A scalar field is kept only if its value is a string, number or boolean.

**Repo URI redaction:**

- `user:password@` userinfo is removed. This covers plain URLs and restic's
  `backend:scheme://` forms such as `rest:https://…` and `s3:https://…`.
- A query string or fragment is replaced with `?[REDACTED]`.
- If the userinfo boundary is ambiguous, everything after `://` becomes `[REDACTED]`. That
  happens when the text before the last `@` contains `/`, `?` or `#`: for example a password
  with an unencoded `/`, or an `@` in the path.
- A URI without `://` that contains `=`, such as an `rclone:` backend with an inline
  connection string, is redacted after its backend prefix.
- Local paths and the short `sftp:user@host:/path` form are returned unchanged. The `user`
  there is an SSH login name, not a secret.

**Never returned:**

- repo passwords (only `hasPassword`)
- env variable values (only the names, in `envKeys`)
- restic `flags` and plan backup flags (only counts)
- hook commands, targets and tokens: webhook URLs, Gotify, Shoutrrr and Telegram
  credentials, and every other field of a hook action
- user password hashes (`passwordBcrypt`)
- the multihost `sync` block (instance identity, pairing secrets)
- any field not named in the tables above

> **Security note for users of versions before 0.4.1.** Earlier versions returned
> Backrest's raw config, including each repo's restic password, env values and hook
> tokens. If you called `get_config` on one of those versions, treat the repo password as
> disclosed to that session's transcript and to any LLM tracing that captured it.

## Safety Controls

Backups are critical data. Five controls gate write and destructive operations:

**1. Read-only mode** (default: on)
```
BACKREST_READONLY=true   # no write tools registered (default)
BACKREST_READONLY=false  # enables trigger_backup, do_repo_task, cancel_operation
```

**2. Destructive gate** (default: off)
```
BACKREST_ALLOW_DESTRUCTIVE=false  # forget/restore never registered (default)
BACKREST_ALLOW_DESTRUCTIVE=true   # enables forget_snapshot, restore_snapshot
                                   # requires BACKREST_READONLY=false
```

**3. Forget confirmation token**

`forget_snapshot` requires `confirm=f"FORGET:{snapshot_id}"`. The caller must name the exact snapshot being deleted.

**4. Restore path guard**

`restore_snapshot` validates the `target` path against `BACKREST_RESTORE_ALLOWED_PREFIX` (default: `~/backrest-restore/`) using `os.path.realpath()`. Path traversal attempts are blocked. The prefix itself must be a real directory (not a symlink) owned by the server user, with mode 0700; otherwise the restore is refused. Create it with `mkdir -m 0700 ~/backrest-restore`. After verifying restored files, move them manually.

**5. Audit log**

Set `BACKREST_AUDIT_LOG=/path/to/audit.jsonl` to log all write operations. Credential values are never included.

## Configuration

| Env var | Required | Default | Purpose |
|---------|----------|---------|---------|
| `BACKREST_URL` | no | `http://localhost:9898` | Backrest base URL |
| `BACKREST_USERNAME` | if Backrest auth is on | — | Basic Auth username |
| `BACKREST_PASSWORD` | if Backrest auth is on | — | Basic Auth password |
| `BACKREST_READONLY` | no | `true` | Disable all write tools |
| `BACKREST_ALLOW_DESTRUCTIVE` | no | `false` | Enable forget/restore (requires `BACKREST_READONLY=false`) |
| `BACKREST_RESTORE_ALLOWED_PREFIX` | no | `~/backrest-restore/` | Restore target path guard (see above) |
| `BACKREST_AUDIT_LOG` | no | — | JSONL audit log for write ops |
| `BACKREST_MCP_TRANSPORT` | no | `stdio` | `stdio` or `http` (long-lived PM2 service) |
| `BACKREST_MCP_HTTP_HOST` | no | `127.0.0.1` | Bind host for http mode (non-loopback refused) |
| `BACKREST_MCP_HTTP_PORT` | no | `8626` | Bind port for http mode |
| `BACKREST_MCP_AUTH_TOKEN` | in http mode | — | Bearer token (≥16 chars); the server refuses to start in http mode without it |
| `BACKREST_MCP_ALLOW_NONLOOPBACK` | no | unset | `1`/`true`/`yes` permits a non-loopback http bind (not recommended) |
| `LOG_LEVEL` | no | `INFO` | Logging verbosity |
| `LOG_FILE` | no | stderr | Log file path |

Telemetry variables (`INFLUXDB_*`, `NATS_*`) are listed under [Observability](#observability).

## Install

Requires Python 3.11+ and a reachable Backrest instance (the API targets v1.13.0).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Optional extras: `.[influxdb]` and `.[nats]` (see [Observability](#observability)). For a
long-lived deployment, install from the lock instead (see [Deployment](#deployment-pm2)).

For development/testing, install the locked set CI uses:
```bash
uv sync --frozen --extra dev
uv run --frozen --no-sync pytest --cov=backrest_mcp
```

The live conformance tests run against a real Backrest. `scripts/run_live_conformance.py`
reads the `BACKREST_*` credentials it needs from the file named by `BACKREST_LIVE_ENV_FILE` (default
`~/.secrets/forge.env`) inside the process, so no credential is ever on a command line. Only
`BACKREST_URL`, `BACKREST_USERNAME`, `BACKREST_PASSWORD` and `BACKREST_REPO_PASSWORD` are
taken from that file; `BACKREST_REPO_PASSWORD` enables the check that `get_config` never
returns the repo password.

```bash
.venv/bin/python scripts/run_live_conformance.py
```

| Exit | Meaning |
|------|---------|
| 0 | The live tests ran and passed |
| 1 | A live test failed or errored |
| 2 | The env file could not be read |
| 3 | Every test skipped, which is not a pass |

## Deployment (PM2)

An `ecosystem.config.js` is included for PM2-managed deployment. It reads credentials from
`~/.secrets/forge.env` itself when PM2 loads it (PM2 6.x has no `--env-file` flag), so none
are stored in the config file:

```bash
cd /path/to/backrest-mcp
pm2 start ecosystem.config.js
```

**Install and update from the lock**, so production runs the dependency set CI audits.
`pip install -e .` leaves already-installed dependencies where they are, however old:

```bash
cd /path/to/backrest-mcp
git pull
UV_PROJECT_ENVIRONMENT=.venv uv sync --frozen --extra dev   # dev: the live runner uses pytest
.venv/bin/python -c "import backrest_mcp.server"
uv tool run --from "pip-audit==2.10.1" pip-audit --skip-editable \
  --path "$(.venv/bin/python -c 'import sysconfig; print(sysconfig.get_path("purelib"))')"
pm2 restart backrest-mcp
```

The audit should report nothing except, possibly, `pip` itself: pip is venv tooling outside
`uv.lock`, not a dependency of the server.

The env file must contain `BACKREST_USERNAME`, `BACKREST_PASSWORD`, and (for http mode)
`BACKREST_MCP_AUTH_TOKEN`. Only those three keys are copied from it; any other setting,
including `INFLUXDB_*` and `NATS_*`, goes in the `env` block of `ecosystem.config.js`. That
block defaults safely (`BACKREST_READONLY=true`, `BACKREST_ALLOW_DESTRUCTIVE=false`).

Before the first start, edit the host-specific values in `ecosystem.config.js`: `cwd` (the
checkout), `BACKREST_AUDIT_LOG` and `LOG_FILE`. If you enable `restore_snapshot`, create the
restore prefix first: `mkdir -m 0700 ~/backrest-restore`.

### HTTP transport

`ecosystem.config.js` defaults to `BACKREST_MCP_TRANSPORT=http`, running a long-lived
streamable-http service on `127.0.0.1:8626/mcp`. HTTP mode **fails closed**:

- binds loopback only (non-loopback bind refused unless `BACKREST_MCP_ALLOW_NONLOOPBACK=1`);
- requires a `BACKREST_MCP_AUTH_TOKEN` bearer token of ≥16 chars (generate with
  `python3 -c "import secrets; print(secrets.token_hex(32))"`).

Clients authenticate with `Authorization: Bearer <token>`. Set `BACKREST_MCP_TRANSPORT=stdio`
(or unset it) to fall back to per-turn stdio via the `backrest-mcp` entry point.

### scoped-mcp wiring

The PM2 process is a single shared instance in front of one Backrest. Register it in each
agent's scoped-mcp manifest as an HTTP server (`type: http`) at
`http://127.0.0.1:8626/mcp`, sending the `BACKREST_MCP_AUTH_TOKEN` value as the bearer
token. With the PM2 defaults only the 11 read tools are registered; the write and
destructive tools do not exist on the server, so no manifest can reach them.

## Claude Desktop Config

```json
{
  "mcpServers": {
    "backrest": {
      "command": "/path/to/backrest-mcp/.venv/bin/python",
      "args": ["-m", "backrest_mcp.server"],
      "env": {
        "BACKREST_URL": "http://localhost:9898",
        "BACKREST_USERNAME": "your-username",
        "BACKREST_PASSWORD": "your-password",
        "BACKREST_READONLY": "true"
      }
    }
  }
}
```

Omit `BACKREST_USERNAME` and `BACKREST_PASSWORD` if Backrest auth is disabled.

## TLS

If connecting to an HTTPS endpoint with a private or self-signed CA:

```
REQUESTS_CA_BUNDLE=/path/to/ca.crt
```

httpx respects this env var. Do **not** disable TLS verification.

## Observability

Structured JSON logs via structlog. `LOG_LEVEL` controls verbosity (default: INFO). Set
`LOG_FILE` to write to a file **instead of** stderr — exactly one sink is attached, so under
PM2 the same line is not also duplicated into `error_file`. If `LOG_FILE` cannot be created
the server falls back to stderr and says so on stderr rather than failing to start.

`LOG_LEVEL` applies to this server's own loggers. `httpx`, `httpcore`, `mcp` and `nats` are
held at WARNING regardless, so their wire trace cannot drown the server's own lines.

Metrics backends are optional extras. With the lock-based deploy, add them to the sync
(`uv sync --frozen --extra dev --extra influxdb --extra nats`); with pip, use
`pip install -e ".[influxdb]"` or `".[nats]"`.

Optional InfluxDB metrics (`influxdb` extra):

| Env var | Purpose |
|---------|---------|
| `INFLUXDB_URL` | InfluxDB write URL. Unset = disabled. |
| `INFLUXDB_TOKEN` | Auth token |
| `INFLUXDB_BUCKET` | Bucket/database (default: `backrest-mcp`) |

Optional NATS publishing (`nats` extra):

| Env var | Purpose |
|---------|---------|
| `NATS_URL` | NATS server URL. Unset = disabled. |
| `NATS_SUBJECT_PREFIX` | Subject prefix (default: `backrest`); subjects are `<prefix>.tool.<tool>` |
| `NATS_CREDS_FILE` | `.creds` file (JWT + nkey). Takes precedence over user/password. |
| `NATS_USER` / `NATS_PASSWORD` | Per-agent username and password. Set both, or neither. |

Userinfo in `NATS_URL` (`nats://user:pass@host`) also works. Setting only one of
`NATS_USER` / `NATS_PASSWORD` logs `nats_auth_incomplete` (variable names only) and connects
without credentials.

Each tool call emits a `backrest_tool` measurement with a `tool` tag and a `duration_ms`
field; list-style tools add `count`, and `get_logs` adds `bytes`. On NATS the message body is
JSON: `{"measurement", "tags", "fields"}`.

There is no OpenTelemetry tracing. An `otel` extra is declared in `pyproject.toml`, but the
server reads no `OTEL_*` variables.

### Failure behaviour

Telemetry is best-effort and never fails a tool call, but it is **not silent**. An unset env
var is the intended disabled state and logs nothing. A backend that is *configured and
failing* warns exactly once per process and is then not retried:

| Event | Meaning |
|-------|---------|
| `influx_init_failed` | `INFLUXDB_URL` set but the client could not be built. Writes disabled for the process. |
| `influx_write_failed` | The client built but a write failed — the usual symptom of a wrong or unreachable URL, since `InfluxDBClient3` connects lazily. Writes keep being attempted. |
| `nats_auth_incomplete` | Only one of `NATS_USER` / `NATS_PASSWORD` is set. The connect goes ahead without credentials. |
| `nats_init_failed` | `NATS_URL` set but the connection failed. Publishes disabled for the process. |
| `nats_transport_error` | First NATS transport error, in place of nats-py's per-attempt ERROR reporting. |
| `nats_publish_failed` | The connection succeeded but a publish failed. Publishes keep being attempted. |

The failure events carry the exception *class* only, never the URL or token — a NATS URL
embeds its credentials.

NATS connects fail fast (`allow_reconnect=False`, two attempts, 2 s connect timeout, 5 s
overall deadline) so an unreachable broker delays a tool call by well under a second rather
than the ~120 s the library defaults produce.

## Auth Architecture

Credentials flow: env vars → `BackrestClient.__init__` → httpx Basic Auth tuple → Authorization header.
Credentials are never written to logs, audit entries, or MCP tool responses.
