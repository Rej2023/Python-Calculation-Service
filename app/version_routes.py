"""
POST /calculate/version-sync — calculation for the .NET version copy/delete job.

Completely independent of /calculate and /calculate/sync (metric change / live saves):
its own models, its own staging table access, its own log tag ("[version job N scope M]")
and its own endpoint. Registered with two lines in main.py; nothing existing is modified.

Reused unchanged (read-only): calculate_all (pure arithmetic), the client connection
lookup, the dependency-edge read and the master CalculationLogs writer.
"""
import datetime
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor

import pyodbc
from fastapi import APIRouter, Header, HTTPException

from app.calculator import calculate_all
from app.database import (get_connection_string_from_master, get_metric_dependency_edges,
                          log_calculation_complete)
from app.version_database import get_version_staging_rows, save_version_staging_amounts
from app.version_models import VersionSyncIssue, VersionSyncRequest, VersionSyncResponse

logger = logging.getLogger(__name__)
router = APIRouter()

# SQLSTATEs that mean "try again later": deadlock, timeouts, lost/failed connection.
_TRANSIENT_SQLSTATES = {"40001", "HYT00", "HYT01", "08S01", "08001", "08004", "08007"}

# Master-DB log writes the caller shouldn't wait for (same idea as main.py's, kept separate).
_log_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="version-calc-log")


def _require_api_key(x_api_key: str | None) -> None:
    # Never log the expected or the supplied key — the log file is readable by anyone
    # with server access.
    if x_api_key != os.getenv("API_KEY"):
        logger.warning("[version-sync] Unauthorized access attempt (invalid or missing X-API-KEY).")
        raise HTTPException(status_code=401, detail="Unauthorized")


def _classify_error(error: BaseException) -> str:
    """"transient" if a retry could succeed (DB connection drop, timeout, deadlock),
    else "permanent" (circular formula, missing data, bad input). Walks the exception chain."""
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, (pyodbc.OperationalError, pyodbc.InterfaceError, TimeoutError, ConnectionError)):
            return "transient"
        if isinstance(current, pyodbc.Error) and current.args and str(current.args[0]) in _TRANSIENT_SQLSTATES:
            return "transient"
        current = current.__cause__ or current.__context__
    return "permanent"


def _log_in_background(tag: str, fn, *args, **kwargs) -> None:
    def run():
        try:
            fn(*args, **kwargs)
        except Exception as log_error:
            logger.error(f"{tag} Failed to write CalculationLogs: {log_error}")
    _log_executor.submit(run)


@router.post("/calculate/version-sync", response_model=VersionSyncResponse)
def calculate_version_sync(
    request: VersionSyncRequest,
    x_api_key: str = Header(None, alias="X-API-KEY"),
):
    """
    Synchronous calculation of ONE sheet of a version copy/delete job, in staging.
    Runs the whole calculation and returns the outcome in the response — no callback,
    so a lost callback can never silently skip the next step.

    Plain `def` on purpose: pyodbc is blocking, so FastAPI runs this in its threadpool
    instead of stalling the event loop for other requests.

    Always HTTP 200 once authenticated; success / errorKind in the body tell the worker
    whether to continue, retry (transient) or fail the job (permanent).
    """
    _require_api_key(x_api_key)

    tag = (f"[{request.clientCode}] [version job {request.jobId} scope {request.scopeId}"
           f"{' ' + request.action if request.action else ''}]")
    response = VersionSyncResponse(success=False, message="", jobId=request.jobId, scopeId=request.scopeId)

    master_conn_str = os.getenv("MASTER_DB_CONNECTION")
    started_at_utc = datetime.datetime.utcnow()
    started = time.perf_counter()

    def elapsed_ms() -> int:
        return int((time.perf_counter() - started) * 1000)

    def finish_log(status: str, message: str, rows_processed: int | None = None) -> None:
        # One finished row (start + outcome) off the critical path — the response doesn't
        # wait for the master DB. Prefixed so these rows are distinguishable in CalculationLogs.
        _log_in_background(tag, log_calculation_complete, master_conn_str, request.clientCode,
                           request.entityId, request.versionId, request.yearId, status,
                           f"[version job {request.jobId} scope {request.scopeId}] {message}",
                           rows_processed, started_at_utc)

    logger.info(f"{tag} Request received (entity {request.entityId}, version {request.versionId}, "
                f"year {request.yearId}, time period {request.timePeriodId}"
                f"{', user ' + request.userId if request.userId else ''}).")

    try:
        logger.info(f"{tag} Resolving client connection...")
        conn_str = get_connection_string_from_master(request.clientCode)

        logger.info(f"{tag} Fetching staging rows...")
        rows = get_version_staging_rows(conn_str, request.jobId, request.scopeId)

        if not rows:
            # The worker stages every scope's rows before calling — none means something
            # upstream went wrong (staging discarded, wrong job/scope id).
            response.errorKind = "permanent"
            response.message = "No staging rows found for this version job scope"
            logger.warning(f"{tag} {response.message}")
            finish_log('FAILED', response.message)
            return response

        # calculate_all assumes one row per (metric, period). Staging is built that way;
        # if it ever is not, fail loudly instead of silently calculating on duplicates.
        keys = [(r["MetricId"], r["TimeId"]) for r in rows]
        if len(keys) != len(set(keys)):
            response.errorKind = "permanent"
            response.message = "Duplicate staging rows found for the same metric and period"
            logger.error(f"{tag} {response.message} ({len(keys) - len(set(keys))} duplicate(s)).")
            finish_log('FAILED', response.message)
            return response

        logger.info(f"{tag} Fetching dependency edges...")
        dependency_edges = get_metric_dependency_edges(conn_str)

        logger.info(f"{tag} Calculating {len(rows)} rows...")
        updates, total_updates, issues = calculate_all(rows, dependency_edges)

        logger.info(f"{tag} Saving {len(updates)} calculated rows and {len(total_updates)} totals to staging...")
        metrics_with_total = save_version_staging_amounts(
            conn_str, request.jobId, request.scopeId, updates, total_updates)

        response.issues = [
            VersionSyncIssue(level=i["level"], message=i["message"], metricId=i.get("metric_id"),
                             metricCode=i.get("metric_code"), timeId=i.get("time_id"))
            for i in issues
        ]
        response.issueCount = len(issues)
        response.errorCount = sum(1 for i in issues if i["level"] == "error")
        response.rowsUpdated = len(updates)
        response.metricsTotalled = metrics_with_total
        response.success = True
        response.message = f"Calculation complete. {len(updates)} rows updated."
        if issues:
            response.message += f" {len(issues)} issue(s), {response.errorCount} error(s)."
            for issue in issues:
                logger.warning(f"{tag} [{issue['level']}] {issue['message']}")

        finish_log('SUCCESS', response.message, len(updates))
        logger.info(f"{tag} Done in {elapsed_ms()} ms ({len(updates)} rows, {metrics_with_total} totals).")
        return response

    except Exception as e:
        response.success = False
        response.errorKind = _classify_error(e)
        response.message = str(e)
        logger.error(f"{tag} {response.errorKind} error after {elapsed_ms()} ms: {e}")
        finish_log('FAILED', f"[{response.errorKind}] {e}")
        return response
