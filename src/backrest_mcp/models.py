"""
Pydantic models for Backrest connect-rpc API responses.

Field names use camelCase to match the connect-rpc JSON encoding (proto field names).
Reconciled against the deployed Backrest v1.13.0 API (proto/v1/service.proto @ a389bbc7);
the upstream `main` proto has since renamed some fields, but this server targets the
version running on forge. See CHANGELOG 0.3.0 for the field-name audit.
"""

from __future__ import annotations

from pydantic import BaseModel


class Operation(BaseModel):
    id: str | None = None
    planId: str | None = None
    repoId: str | None = None
    repoGuid: str | None = None
    snapshotId: str | None = None
    status: str | None = None
    unixTimeStartMs: int | None = None
    unixTimeEndMs: int | None = None
    displayMessage: str | None = None
    # Reference to this operation's log stream; pass to get_logs(ref=...) to read it.
    logref: str | None = None


class OperationList(BaseModel):
    operations: list[Operation] = []


class Snapshot(BaseModel):
    id: str | None = None
    unixTimeMs: int | None = None
    hostname: str | None = None
    paths: list[str] | None = None
    tags: list[str] | None = None
    # Populated by list_snapshots when merging across repos (not an API field).
    repoId: str | None = None


class SnapshotList(BaseModel):
    snapshots: list[Snapshot] = []


class LsEntry(BaseModel):
    name: str | None = None
    type: str | None = None
    path: str | None = None
    size: int | None = None
    mtime: str | None = None


class ListSnapshotFilesResponse(BaseModel):
    path: str | None = None
    entries: list[LsEntry] = []


class Summary(BaseModel):
    """A repo or plan dashboard summary (SummaryDashboardResponse.Summary).

    Repo and plan summaries share the same proto message, so one model covers both.
    Field names match Backrest v1.13.0 JSON encoding.
    """

    id: str | None = None
    backupsFailed30days: int | None = None
    backupsWarningLast30days: int | None = None
    backupsSuccessLast30days: int | None = None
    bytesScannedLast30days: int | None = None
    bytesAddedLast30days: int | None = None
    totalSnapshots: int | None = None
    bytesScannedAvg: int | None = None
    bytesAddedAvg: int | None = None
    nextBackupTimeMs: int | None = None


class SummaryDashboard(BaseModel):
    repoSummaries: list[Summary] = []
    planSummaries: list[Summary] = []
    configPath: str | None = None
    dataPath: str | None = None
