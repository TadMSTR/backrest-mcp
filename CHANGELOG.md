# Changelog

## [0.4.0] — 2026-08-30

Telemetry that fails is now visible. Ports the fixes proven in dockhand-mcp v0.4.0
(TadMSTR/dockhand-mcp#6) to this server — vikunja#575 item 3, #580.

Everything here was verified against **genuinely dead backends**, not mocks: both of the
defects in #580 were invisible to mocked tests. Measured on 2026-08-30 with this repo's
own code, `INFLUXDB_URL=http://127.0.0.1:9/nope` and `NATS_URL` pointed at a refused port:

| | before | after |
|---|---|---|
| wall clock per `emit_metric` | 120.2 s | < 0.08 s (10 calls total) |
| this server's own log lines | 0 | 3 |
| `nats.aio.client` ERROR records | 61 per call | 0 |

### Fixed

- **A configured-but-failing backend was completely silent.** `_get_influx()` and
  `_get_nats()` swallowed every exception with `except Exception: pass`, leaving the client
  global at `None`. Two consequences: nothing in the logs ever mentioned the broken backend,
  and every subsequent `emit_metric()` re-entered the connect path. Each now warns exactly
  once and sets a negative-cache sentinel. An *unset* env var remains the intended disabled
  state and stays silent — "never tried" and "tried and failed" are distinct flags, not an
  overloaded `None`.

- **The InfluxDB write path was the one that actually mattered** (#580). `InfluxDBClient3`
  constructs lazily and never contacts the host, so against an unreachable URL
  `_get_influx()` *succeeds* and an init-only sentinel never fires — the failure surfaces
  only on `write()`, which had its own `except Exception: pass`. `emit_metric` now warns
  once on a failed write (and once on a failed NATS publish) while continuing to attempt
  them, since a write failure is often transient.

- **One `nats.connect()` blocked a tool call for ~120 s and logged 61 times.** nats-py
  defaults to `max_reconnect_attempts=60` / `reconnect_time_wait=2` and reports every failed
  attempt through `error_cb` — at ERROR, so the WARNING demotion below does not silence it.
  Connects now use `allow_reconnect=False`, `max_reconnect_attempts=1`,
  `reconnect_time_wait=0`, `connect_timeout=2`, an overall 5 s `asyncio.wait_for` deadline,
  and a replacement `error_cb` that warns once. (`max_reconnect_attempts=0` is a trap — the
  discard check is `> 0`, so 0 retries forever. 1 is the fail-fast value.)

- **Third-party wire trace drowned the server's own logs.** The root logger was set to
  `LOG_LEVEL` with no demotions, so `httpx`/`httpcore`/`mcp`/`nats` all inherited INFO.
  Measured in `backrest-mcp.log` on 2026-08-30: 51,905 third-party lines against 6 of ours
  (`mcp.server.lowlevel.server` 21,088; `mcp.server.streamable_http_manager` 10,282;
  `mcp.server.streamable_http` 10,273; `httpx` 10,262). Those four prefixes are now held at
  WARNING; the `backrest_mcp` logger still honours `LOG_LEVEL`.

- **Every log line was written twice.** A stderr `StreamHandler` and a `FileHandler` were
  both attached, so under PM2 the same content landed in `LOG_FILE` *and* in
  `~/.pm2/logs/backrest-mcp-error.log` — 8.5 MB and 9.5 MB of near-identical unrotated log,
  per-logger counts matching within ~500 lines. One sink is now attached: the file handler
  when `LOG_FILE` is writable, stderr otherwise. The `OSError` fallback is preserved, so an
  unwritable log path degrades to stderr instead of crashing startup. The README's claim
  that `LOG_FILE` writes "to a file instead of stderr" is true for the first time.

### Security

- The new warnings carry the exception *class* only, never `str(exc)`, the URL or the token.
  A NATS URL is `nats://user:password@host` and an InfluxDB error can echo the host or token,
  so a build that makes telemetry louder must not make it leakier. No log site in this module
  uses `exc_info=True`; a test enforces that. (This follows the dockhand-mcp audit split from
  2026-08-29 LOW-1, where the two OTel sites kept `exc_info` because their config carries no
  credential — this server has no OTel path, so the exempt set here is empty.)

### Tests

- `tests/test_observability.py` rewritten from a 3-test smoke file to 23 tests covering the
  sentinels, the write/publish warnings, the fail-fast NATS options, the single sink, the
  logger demotion and the no-credential-in-logs rule. `backrest_mcp/observability.py` goes
  from partial to 100% statement coverage. The optional backends are faked via `sys.modules`
  rather than imported, because CI installs only `.[dev]` — a test that needed the real
  package would skip exactly where it matters most.

## [0.3.0] — 2026-07-25

Reconciled the mock-only v0.2.1 against the **deployed Backrest v1.13.0** connect-rpc API
(forge `:9898`). The prior release was written and tested entirely against hand-mocked
JSON and had never been run against a live Backrest; several field-name mismatches only
surface against the real API. Field names in this release target the **deployed** version,
not upstream `main` (which has since diverged).

### Fixed

- **Broken standalone entrypoint** — added the missing `if __name__ == "__main__": main()`
  guard. `python -m backrest_mcp.server` (used by PM2 and the Claude Desktop snippet) was a
  silent no-op: it imported the module and exited without starting the server.
- **`get_summary` failed-count field** — model used `backupsFailedLast30days`; the API field
  is `backupsFailed30days`, so failed-backup counts always rendered as absent. Corrected and
  added `backupsWarningLast30days`, `bytesScannedLast30days`, and the byte-average fields.
- **`get_operations` repo filter** — the operation selector keyed on `repoId`, but
  `OpSelector` matches on `repoGuid`, so repo filtering was silently ignored. `repo_id` is
  now resolved to its GUID. Also: with no filter, GetOperations returns nothing, so the tool
  now fans out across all configured repos.
- **`list_snapshots()` with no arguments** returned HTTP 500 (empty `repo_id` rejected). It
  now enumerates configured repos and merges results, tagging each snapshot with its
  `repoId`. (Backrest #223)
- **Stale server instructions** — the FastMCP `instructions=` text claiming Backrest was "not
  yet deployed" was rewritten for the live deployment.

### Added

- **`get_health`** — wraps GetConfig; returns `ok` / `auth_failed` / `unreachable` plus the
  configured URL and repo/plan counts. Safe to poll. (Backrest #222)
- **`get_logs`** — reads an operation's log output over the Connect server-streaming
  protocol (the primary tool for diagnosing a failed backup). The log reference is surfaced
  by `get_operations` so an agent can chain `get_operations` → `get_logs`.
- **`get_download_url`** — signed download URL for a file from a restore operation.
- **HTTP/PM2 transport** — `BACKREST_MCP_TRANSPORT=http` runs a long-lived streamable-http
  service (loopback-only bind, mandatory `StaticTokenVerifier` bearer token). `main()` fails
  closed on a non-loopback bind or a missing/short token. `stdio` remains the default.
- **Live conformance tests** (`tests/test_live_conformance.py`, gated by `BACKREST_LIVE_TEST=1`
  so CI stays hermetic) that exercise the real `:9898` API.
- **CI** (`.github/workflows/ci.yml`: ruff + pytest matrix on 3.11/3.12/3.13), ruff config,
  and `otel` / `nats` optional-dependency extras.

### Changed

- **`list_snapshot_files` argument `repo_guid` → `repo_id`.** The tool now accepts the human
  repo ID (consistent with every other tool) and resolves it to the GUID internally. NOTE:
  the underlying v1.13.0 `ListSnapshotFilesRequest` field is `repo_guid` and is looked up by
  GUID — the v0.2.1 code sending `repoGuid` was already correct against the deployed version;
  only the exposed argument name and internal resolution changed.
- `mcp.run(transport="stdio")` → `mcp.run()` so the transport is FastMCP-configurable.
- Coverage raised to ~83% (from 57%).

### Security

- **Connect-stream decoder hardening** (audit `backrest-mcp-modernization-2026-07`, Low) —
  `post_streaming` now bounds the buffered response at 64 MiB, and `_decode_connect_stream`
  raises `BackrestStreamError` on a frame whose declared length overruns the buffer instead
  of silently returning a truncated log body.

### Explicitly excluded

- `RunCommand`, `SetConfig`, `AddRepo`, `RemoveRepo`, `ClearHistory` remain unregistered.

## [0.2.1] — 2026-06-04

### Security

- Fixed restore path guard sibling-directory bypass — replaced `startswith()` with
  `Path.is_relative_to()` to correctly reject paths like `/tmp/backrest-restore-evil/`
  when allowed prefix is `/tmp/backrest-restore/` (F-01)
- Added `validate_backrest_id()` to `repo_guid` parameter in `list_snapshot_files` —
  completes consistent ID validation across all 10 tools (F-02)
- Added upper bounds to all runtime dependencies to prevent silent major-version adoption:
  `fastmcp>=3.0,<4.0`, `httpx>=0.27,<1.0`, `pydantic>=2.0,<3.0`, `structlog>=24.0,<27.0` (F-04)
- Added sibling-prefix bypass test `test_restore_sibling_prefix_rejected` (F-03)

## [0.2.0] — 2026-06-04

### Changed

- Rewritten from TypeScript/Node to Python/FastMCP to match forge MCP standard
- Renamed from `backrest-mcp-server` to `backrest-mcp`

### Added

- `get_config` — read Backrest config (repos, plans)
- `list_snapshots` — list snapshots for a repo or plan
- `list_snapshot_files` — browse files within a snapshot
- `get_summary` — 30-day dashboard stats (success/fail counts, bytes added)
- `do_repo_task` — trigger prune/check/stats/unlock/index on a repo
- `forget_snapshot` — forget a specific snapshot (requires ALLOW_DESTRUCTIVE + confirm token)
- `restore_snapshot` — restore a snapshot to a staging path (requires ALLOW_DESTRUCTIVE + path guard)
- `cancel_operation` — cancel a running operation
- `trigger_backup` gains `dry_run` parameter
- `safety.py` — layered safety controls: READONLY mode, ALLOW_DESTRUCTIVE gate, restore path
  guard, forget confirmation token, audit log
- `ecosystem.config.js` — PM2 config with safe defaults (READONLY=true)
- `observability.py` — structlog JSON logging + optional InfluxDB metrics
- `tests/` — pytest + respx mocks (test_client, test_tools, test_safety)

## [0.1.1] — 2026-03-12

### Security

- **Input validation hardened** — plan IDs and repo IDs now validated with zod regex schema
  (`/^[\w\-]+$/`, 1–128 chars) before use in API requests; rejects path traversal and
  injection characters.
- **`zod` pinned** — explicit version pin in `package.json` to prevent supply-chain drift.
- **`.env.example` added** — documents required env vars without shipping real credentials.
- **TLS documentation** — README updated with self-signed cert and mTLS guidance for
  non-default Backrest deployments.

## [0.1.0] — 2026-03-09

### Added

- Initial release of `backrest-mcp-server` — TypeScript MCP server (stdio) wrapping the
  Backrest backup manager REST API
- `trigger-backup(planId)` — POST to Backrest `/v1.Backrest/Backup`; blocks until completion
- `get-operations(planId?, repoId?, limit)` — Fetch recent operation history with optional
  plan/repo filter
- Basic Auth support via `BACKREST_USERNAME` / `BACKREST_PASSWORD` env vars (optional)
- Env vars: `BACKREST_URL` (default: `http://localhost:9898`), `BACKREST_USERNAME`, `BACKREST_PASSWORD`
