"""
Pydantic models for Backrest connect-rpc API responses.

Field names use camelCase to match the connect-rpc JSON encoding (proto field names).
Reconciled against the deployed Backrest v1.13.0 API (proto/v1/service.proto @ a389bbc7);
the upstream `main` proto has since renamed some fields, but this server targets the
version running on forge. See CHANGELOG 0.3.0 for the field-name audit.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel


class Operation(BaseModel):
    id: Optional[str] = None
    planId: Optional[str] = None
    repoId: Optional[str] = None
    repoGuid: Optional[str] = None
    snapshotId: Optional[str] = None
    status: Optional[str] = None
    unixTimeStartMs: Optional[int] = None
    unixTimeEndMs: Optional[int] = None
    displayMessage: Optional[str] = None
    # Reference to this operation's log stream; pass to get_logs(ref=...) to read it.
    logref: Optional[str] = None


class OperationList(BaseModel):
    operations: List[Operation] = []


class Snapshot(BaseModel):
    id: Optional[str] = None
    unixTimeMs: Optional[int] = None
    hostname: Optional[str] = None
    paths: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    # Populated by list_snapshots when merging across repos (not an API field).
    repoId: Optional[str] = None


class SnapshotList(BaseModel):
    snapshots: List[Snapshot] = []


class LsEntry(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    path: Optional[str] = None
    size: Optional[int] = None
    mtime: Optional[str] = None


class ListSnapshotFilesResponse(BaseModel):
    path: Optional[str] = None
    entries: List[LsEntry] = []


class Summary(BaseModel):
    """A repo or plan dashboard summary (SummaryDashboardResponse.Summary).

    Repo and plan summaries share the same proto message, so one model covers both.
    Field names match Backrest v1.13.0 JSON encoding.
    """

    id: Optional[str] = None
    backupsFailed30days: Optional[int] = None
    backupsWarningLast30days: Optional[int] = None
    backupsSuccessLast30days: Optional[int] = None
    bytesScannedLast30days: Optional[int] = None
    bytesAddedLast30days: Optional[int] = None
    totalSnapshots: Optional[int] = None
    bytesScannedAvg: Optional[int] = None
    bytesAddedAvg: Optional[int] = None
    nextBackupTimeMs: Optional[int] = None


class SummaryDashboard(BaseModel):
    repoSummaries: List[Summary] = []
    planSummaries: List[Summary] = []
    configPath: Optional[str] = None
    dataPath: Optional[str] = None
