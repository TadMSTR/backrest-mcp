# Changelog

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
