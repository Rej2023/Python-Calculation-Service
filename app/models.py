from pydantic import BaseModel
from typing import Optional

class CalculationRequest(BaseModel):
    clientCode: str
    entityId: int
    versionId: int
    yearId: int
    timePeriodId: int
    productId: Optional[int] = None
    projectId: Optional[int] = None
    callbackUrl: str
    userId: Optional[str] = None

class CalculationResponse(BaseModel):
    success: bool
    message: str
    clientCode: str
    entityId: int
    versionId: int
    yearId: int
    issueCount: int = 0
    issues: list[str] = []
    # Echoed back from the request so the .NET callback (which has no HTTP
    # context of its own here — this is a server-to-server call from Python,
    # not a user request) knows the exact scope to consolidate and who to
    # attribute it to, without needing to track in-flight requests itself.
    timePeriodId: int = 0
    productId: Optional[int] = None
    projectId: Optional[int] = None
    userId: Optional[str] = None


class SyncCalculationRequest(BaseModel):
    """
    /calculate/sync — called by the .NET background worker, which waits for
    the result instead of relying on a callback.

    Job mode (jobId + scopeId set): reads and writes SheetDataStaging for that
    job's scope only — live SheetData is never touched. The worker sends the
    job's NEW structure, because the live Metrics/MetricDependencies tables
    still hold the old one until the job commits:
      - dependencyEdges: [[depends_on_code, metric_code], ...]
      - timeBehaviors:   {metric_id: time_behavior}
    Formulas come from the staging rows themselves (the worker writes the new
    formula onto them).

    Live mode (no jobId): same calculation as /calculate on live SheetData,
    just synchronous — used for everyday saves routed through the worker.
    """
    clientCode: str
    entityId: int
    versionId: int
    yearId: int
    timePeriodId: int
    jobId: Optional[int] = None
    scopeId: Optional[int] = None
    dependencyEdges: Optional[list[list[str]]] = None
    timeBehaviors: Optional[dict[int, int]] = None
    userId: Optional[str] = None


class CalculationIssue(BaseModel):
    level: str          # "warning" (auto-handled, e.g. divide-by-zero) | "error" (cell defaulted to 0)
    message: str
    metricId: Optional[int] = None
    metricCode: Optional[str] = None
    timeId: Optional[int] = None


class SyncCalculationResponse(BaseModel):
    success: bool
    message: str
    # Set only when success is False:
    #   "transient" — worth retrying (DB connection/timeout/deadlock)
    #   "permanent" — will fail the same way every time (circular formula, bad data)
    errorKind: Optional[str] = None
    rowsUpdated: int = 0
    issueCount: int = 0
    errorCount: int = 0
    issues: list[CalculationIssue] = []
    jobId: Optional[int] = None
    scopeId: Optional[int] = None