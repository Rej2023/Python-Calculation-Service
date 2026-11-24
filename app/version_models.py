"""
Request/response models for POST /calculate/version-sync.

Kept in their own module on purpose: the version copy/delete calculation is
completely independent of the metric-change one (app/models.py is untouched).
"""
from typing import Literal, Optional

from pydantic import BaseModel


class VersionSyncRequest(BaseModel):
    """
    Called by the .NET version job worker, once per calculation scope
    (one Entity / Version / Year / TimePeriod sheet), and waits for the result.

    Reads and writes VersionJobSheetStaging for that job's scope ONLY — live
    SheetData is never touched. A version copy/delete never changes the metric
    structure, so formulas come from the staging rows (the worker writes the
    current rule expression onto them) and the dependency edges / time
    behaviours come from the live MetricDependencies / Metrics tables.
    """
    clientCode: str
    entityId: int
    versionId: int
    yearId: int
    timePeriodId: int
    jobId: int
    scopeId: int
    # Only used to label the log lines.
    action: Optional[Literal["copy", "delete"]] = None
    userId: Optional[str] = None


class VersionSyncIssue(BaseModel):
    level: str          # "warning" (auto-handled, e.g. divide-by-zero) | "error" (cell defaulted to 0)
    message: str
    metricId: Optional[int] = None
    metricCode: Optional[str] = None
    timeId: Optional[int] = None


class VersionSyncResponse(BaseModel):
    success: bool
    message: str
    # Set only when success is False:
    #   "transient" — worth retrying (DB connection/timeout/deadlock)
    #   "permanent" — will fail the same way every time (circular formula, bad data)
    errorKind: Optional[str] = None
    rowsUpdated: int = 0
    metricsTotalled: int = 0
    issueCount: int = 0
    errorCount: int = 0
    issues: list[VersionSyncIssue] = []
    jobId: Optional[int] = None
    scopeId: Optional[int] = None
