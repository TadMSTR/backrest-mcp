# backrest-mcp

Python/FastMCP MCP server (stdio or loopback HTTP transport) wrapping the Backrest backup
manager REST API. Field names target the **deployed Backrest v1.13.0** connect-rpc API
(upstream `main` has since diverged — see `CHANGELOG.md` 0.3.0).

## What it does

Provides MCP tools for querying backup operations, listing snapshots, reading config,
browsing snapshot contents, triggering backups, running repo maintenance tasks, and
(with explicit opt-in) forgetting snapshots and restoring data to staging paths.

## Tools

**Always registered (read-only):**
- `get_health()` — Reachability + credential health (`ok`/`auth_failed`/`unreachable`); poll-safe
- `get_config()` — Read Backrest configuration (repos, plans, global settings)
- `list_snapshots(repo_id?, plan_id?)` — List snapshots; no args → enumerate all repos, tag each
- `list_snapshot_files(repo_id, snapshot_id, path?)` — Browse files (repo_id resolved to GUID internally)
- `get_summary()` — 30-day dashboard stats per repo and plan
- `get_operations(plan_id?, repo_id?, limit?)` — Recent operations with status icons + log refs
- `get_logs(ref, max_bytes?)` — Read an operation's log (ref surfaced by `get_operations`; Connect stream)
- `get_download_url(operation_id, file_path)` — Signed URL for a restored file

**Registered when `BACKREST_READONLY=false`:**
- `trigger_backup(plan_id, dry_run?)` — Trigger a backup plan
- `do_repo_task(repo_id, task)` — Run maintenance: index/prune/check/stats/unlock/forget
- `cancel_operation(operation_id)` — Cancel a running operation

**Registered when `BACKREST_ALLOW_DESTRUCTIVE=true` (requires `READONLY=false`):**
- `forget_snapshot(snapshot_id, repo_id, confirm, plan_id?)` — Permanently forget a snapshot
- `restore_snapshot(snapshot_id, repo_id, path, target, plan_id?)` — Restore to staging path

**Intentionally NOT exposed** (dangerous — keep unregistered): `RunCommand` (arbitrary
restic execution), `SetConfig`, `AddRepo`, `RemoveRepo` (deletes history), `ClearHistory`.

## Structure

```
backrest_mcp/
  __init__.py
  client.py        BackrestClient — connect-rpc: unary POST+JSON, plus post_streaming() for GetLogs
  models.py        Pydantic models for API responses (camelCase field names, v1.13.0)
  safety.py        Safety controls — READONLY flag, ALLOW_DESTRUCTIVE gate, path guard, audit log
  server.py        FastMCP server — conditional tool registration, transport select + fail-closed guard
  observability.py structlog JSON logging + optional InfluxDB/NATS metrics
tests/
  test_client.py           HTTP mechanics, auth, error handling, Connect streaming decode
  test_tools.py            End-to-end read-tool tests via FastMCP direct-call API
  test_safety.py           Safety gating, confirm tokens, path guards, write-tool happy paths
  test_http_transport.py   HTTP transport fail-closed guards (loopback + bearer token)
  test_observability.py    Logging config + no-op metric smoke tests
  test_live_conformance.py Live :9898 conformance (skipped unless BACKREST_LIVE_TEST=1)
pyproject.toml
ecosystem.config.js  PM2 config (HTTP transport, BACKREST_READONLY=true default)
```

## Configuration

| Env var | Default | Purpose |
|---------|---------|---------|
| `BACKREST_URL` | `http://localhost:9898` | Backrest base URL |
| `BACKREST_USERNAME` | — | Basic Auth username (optional) |
| `BACKREST_PASSWORD` | — | Basic Auth password (optional) |
| `BACKREST_READONLY` | `true` | Disable all write tools when true |
| `BACKREST_ALLOW_DESTRUCTIVE` | `false` | Enable forget_snapshot + restore_snapshot |
| `BACKREST_RESTORE_ALLOWED_PREFIX` | `/tmp/backrest-restore/` | Restore target path guard |
| `BACKREST_AUDIT_LOG` | — | JSONL audit log path for write ops |
| `BACKREST_MCP_TRANSPORT` | `stdio` | `stdio` or `http` (loopback PM2 service) |
| `BACKREST_MCP_HTTP_HOST` | `127.0.0.1` | http bind host (non-loopback refused unless override) |
| `BACKREST_MCP_HTTP_PORT` | `8626` | http bind port |
| `BACKREST_MCP_AUTH_TOKEN` | — | Bearer token, required in http mode (≥16 chars) |
| `BACKREST_MCP_ALLOW_NONLOOPBACK` | `false` | Permit non-loopback http bind (not recommended) |
| `LOG_LEVEL` | `INFO` | Logging verbosity |
| `LOG_FILE` | stderr | Log file path |
| `INFLUXDB_URL` | — | InfluxDB for metrics (optional) |

## Safety controls (`safety.py`)

Four layered controls gate write and destructive operations:

1. **READONLY flag** — `BACKREST_READONLY=true` (default) prevents write tools from being
   registered at startup. No write calls are possible.

2. **ALLOW_DESTRUCTIVE gate** — `BACKREST_ALLOW_DESTRUCTIVE=false` (default) prevents
   `forget_snapshot` and `restore_snapshot` from being registered even when READONLY=false.

3. **Forget confirmation token** — `forget_snapshot` requires `confirm=f"FORGET:{snapshot_id}"`.
   Forces the caller to name the exact snapshot being deleted.

4. **Restore path guard** — `restore_snapshot` validates `target` with `os.path.realpath()`
   against `BACKREST_RESTORE_ALLOWED_PREFIX`. Path traversal attempts are blocked.

5. **Audit log** — all write tool calls append a JSONL entry to `BACKREST_AUDIT_LOG` (if set).
   Credential values are never included.

## Key architecture decisions

- **Connect-rpc-over-HTTP** — unary Backrest calls are `POST {base_url}/v1.Backrest/{Method}`
  with a JSON body. `GetLogs` is a Connect **server-streaming** RPC: `post_streaming()` sends an
  enveloped `application/connect+json` frame and decodes the base64 `BytesValue` frames.
- **Deployed-version targeting** — reconciled against Backrest v1.13.0. `ListSnapshotFiles` and
  `OpSelector` key on the repo **GUID**; tools accept the human `repo_id` and resolve it via
  GetConfig so the interface is uniform. No-arg `list_snapshots`/`get_operations` enumerate repos.
- **HTTP transport fails closed** — `main()` refuses a non-loopback bind or a missing/short
  bearer token; `StaticTokenVerifier` gates the endpoint.
- **No credentials in logs** — `BACKREST_USERNAME`/`BACKREST_PASSWORD` are used only in the
  httpx Basic Auth tuple and never written to any log or audit entry.
- **lru_cache on get_client()** — single BackrestClient instance reused for the server lifetime.
- **Opt-in write access** — safe default requires no configuration. Set env vars explicitly to unlock.

## Build

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## Git workflow

Branch before editing — do not commit directly to `main`.
Feature branches: `feature/<slug>` or `fix/<slug>`.
