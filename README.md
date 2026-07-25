# backrest-mcp

MCP server for [Backrest](https://github.com/garethgeorge/backrest) — a web UI and orchestrator for restic backups.

Python/FastMCP rewrite of `backrest-mcp-server`. Covers the full useful surface of the Backrest REST API with layered safety controls to protect backup data.

## Tools

| Tool | Description | Requires |
|------|-------------|---------|
| `get_health` | Check reachability + credential health (poll-safe) | — |
| `get_config` | Read Backrest configuration (repos, plans) | — |
| `list_snapshots` | List snapshots; no args → all repos merged | — |
| `list_snapshot_files` | Browse files within a snapshot (by `repo_id`) | — |
| `get_summary` | 30-day dashboard stats per repo and plan | — |
| `get_operations` | Recent operation history with status icons + log refs | — |
| `get_logs` | Read an operation's log output (ref from `get_operations`) | — |
| `get_download_url` | Signed download URL for a restored file | — |
| `trigger_backup` | Trigger a backup plan (dry_run supported) | `BACKREST_READONLY=false` |
| `do_repo_task` | Run maintenance: prune/check/stats/unlock/index | `BACKREST_READONLY=false` |
| `cancel_operation` | Cancel a running operation | `BACKREST_READONLY=false` |
| `forget_snapshot` | Permanently forget a snapshot (confirm token required) | `BACKREST_ALLOW_DESTRUCTIVE=true` |
| `restore_snapshot` | Restore snapshot to a staging path | `BACKREST_ALLOW_DESTRUCTIVE=true` |

Default state: **read-only** — only the 8 read tools are registered. No write calls are
possible without explicit opt-in. `RunCommand`, `SetConfig`, `AddRepo`, `RemoveRepo`, and
`ClearHistory` are intentionally never exposed.

> API field names target the **deployed Backrest v1.13.0** connect-rpc API, which upstream
> `main` has since diverged from. See `CHANGELOG.md` (0.3.0) for the reconciliation notes.

## Safety Controls

Backups are critical data. Four controls gate write and destructive operations:

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

`restore_snapshot` validates the `target` path against `BACKREST_RESTORE_ALLOWED_PREFIX` (default: `/tmp/backrest-restore/`) using `os.path.realpath()`. Path traversal attempts are blocked. After verifying restored files, move them manually.

**5. Audit log**

Set `BACKREST_AUDIT_LOG=/path/to/audit.jsonl` to log all write operations. Credential values are never included.

## Configuration

| Env var | Default | Purpose |
|---------|---------|---------|
| `BACKREST_URL` | `http://localhost:9898` | Backrest base URL |
| `BACKREST_USERNAME` | — | Basic Auth username (optional) |
| `BACKREST_PASSWORD` | — | Basic Auth password |
| `BACKREST_READONLY` | `true` | Disable all write tools |
| `BACKREST_ALLOW_DESTRUCTIVE` | `false` | Enable forget/restore (requires READONLY=false) |
| `BACKREST_RESTORE_ALLOWED_PREFIX` | `/tmp/backrest-restore/` | Restore target path guard |
| `BACKREST_AUDIT_LOG` | — | JSONL audit log for write ops |
| `BACKREST_MCP_TRANSPORT` | `stdio` | `stdio` or `http` (long-lived PM2 service) |
| `BACKREST_MCP_HTTP_HOST` | `127.0.0.1` | Bind host for http mode (non-loopback refused) |
| `BACKREST_MCP_HTTP_PORT` | `8626` | Bind port for http mode |
| `BACKREST_MCP_AUTH_TOKEN` | — | Bearer token, **required** in http mode (≥16 chars) |
| `LOG_LEVEL` | `INFO` | Logging verbosity |
| `LOG_FILE` | stderr | Log file path |
| `INFLUXDB_URL` | — | Optional InfluxDB metrics |

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

For development/testing:
```bash
pip install -e ".[dev]"
pytest
```

## Deployment (PM2)

An `ecosystem.config.js` is included for PM2-managed deployment. Credentials are injected
via `--env-file` to avoid storing them in the config file:

```bash
cd /path/to/backrest-mcp
pm2 start ecosystem.config.js --env-file /path/to/secrets.env
```

The secrets file must contain `BACKREST_USERNAME`, `BACKREST_PASSWORD`, and (for http mode)
`BACKREST_MCP_AUTH_TOKEN`. All other settings default safely in `ecosystem.config.js`
(`BACKREST_READONLY=true`, `BACKREST_ALLOW_DESTRUCTIVE=false`).

### HTTP transport

`ecosystem.config.js` defaults to `BACKREST_MCP_TRANSPORT=http`, running a long-lived
streamable-http service on `127.0.0.1:8626/mcp`. HTTP mode **fails closed**:

- binds loopback only (non-loopback bind refused unless `BACKREST_MCP_ALLOW_NONLOOPBACK=1`);
- requires a `BACKREST_MCP_AUTH_TOKEN` bearer token of ≥16 chars (generate with
  `python3 -c "import secrets; print(secrets.token_hex(32))"`).

Clients authenticate with `Authorization: Bearer <token>`. Set `BACKREST_MCP_TRANSPORT=stdio`
(or unset it) to fall back to per-turn stdio via the `backrest-mcp` entry point.

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
`LOG_FILE` to write to a file instead of stderr.

Optional InfluxDB metrics via `pip install -e ".[influxdb]"`:

| Env var | Purpose |
|---------|---------|
| `INFLUXDB_URL` | InfluxDB write URL |
| `INFLUXDB_TOKEN` | Auth token |
| `INFLUXDB_ORG` | Organization |
| `INFLUXDB_BUCKET` | Bucket (default: `backrest`) |

Each tool call emits a `backrest_tool` measurement with `tool` tag and `duration_ms` field.

## Auth Architecture

Credentials flow: env vars → `BackrestClient.__init__` → httpx Basic Auth tuple → Authorization header.
Credentials are never written to logs, audit entries, or MCP tool responses.
