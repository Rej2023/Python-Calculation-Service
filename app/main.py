import os
import logging
from pathlib import Path
from dotenv import load_dotenv

# FIX 1: Force absolute path for the .env file based on this script's location
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(dotenv_path=BASE_DIR / ".env")
# Optional, untracked (.gitignore): local-only settings such as
# CLIENT_DB_OVERRIDE_<CLIENTCODE>. Overrides .env when present; servers don't have it.
load_dotenv(dotenv_path=BASE_DIR / ".env.local", override=True)

import pyodbc
from fastapi import FastAPI, BackgroundTasks, HTTPException, Header
from app.models import (CalculationRequest, CalculationResponse, SyncCalculationRequest,
                        SyncCalculationResponse, CalculationIssue)
from app.database import (get_connection_string_from_master, get_sheet_data, get_metric_dependency_edges,
                          save_calculated_amounts, log_calculation, log_calculation_complete,
                          get_staging_data, save_staging_amounts)
import datetime
from concurrent.futures import ThreadPoolExecutor
from app.calculator import calculate_all
from app.callback import send_callback

app = FastAPI(title="Calculation Service")

# FIX 2: Ensure the log directory exists relative to the project directory
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "calculation.log") # FIXED: Using absolute path
    ]
)
logger = logging.getLogger(__name__)


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.post("/calculate")
async def calculate(
    request: CalculationRequest,
    background_tasks: BackgroundTasks,
    x_api_key: str = Header(None, alias="X-API-KEY") 
):
    require_api_key(x_api_key)

    background_tasks.add_task(run_calculation, request)
    return {"status": "accepted", "message": "Calculation started"}


def require_api_key(x_api_key: str | None) -> None:
    # Never log the expected key (or the supplied one) — the log file is
    # readable by anyone with server access.
    if x_api_key != os.getenv("API_KEY"):
        logger.warning("Unauthorized access attempt (invalid or missing X-API-KEY).")
        raise HTTPException(status_code=401, detail="Unauthorized")


# SQLSTATEs that mean "try again later": deadlock, timeouts, lost/failed connection.
TRANSIENT_SQLSTATES = {"40001", "HYT00", "HYT01", "08S01", "08001", "08004", "08007"}


def classify_error(error: BaseException) -> str:
    """
    "transient" if a retry could succeed (DB connection drop, timeout,
    deadlock), else "permanent" (circular formula, missing data, bad input) —
    the worker retries only transient failures. Walks the exception chain,
    since database.py wraps pyodbc errors with `raise ... from e`.
    """
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, (pyodbc.OperationalError, pyodbc.InterfaceError, TimeoutError, ConnectionError)):
            return "transient"
        if isinstance(current, pyodbc.Error) and current.args and str(current.args[0]) in TRANSIENT_SQLSTATES:
            return "transient"
        current = current.__cause__ or current.__context__
    return "permanent"


@app.post("/calculate/sync", response_model=SyncCalculationResponse)
def calculate_sync(
    request: SyncCalculationRequest,
    x_api_key: str = Header(None, alias="X-API-KEY")
):
    """
    Synchronous calculation for the .NET background worker: runs the whole
    calculation and returns the outcome in the response — no callback, so a
    lost callback can never silently skip the next step.

    Plain `def` on purpose: pyodbc is blocking, so FastAPI runs this in its
    threadpool instead of stalling the event loop for other requests.

    Always HTTP 200 once authenticated; success/errorKind in the body tell the
    worker whether to continue, retry (transient) or fail the job (permanent).
    """
    require_api_key(x_api_key)

    is_job = request.jobId is not None
    if is_job and request.scopeId is None:
        raise HTTPException(status_code=400, detail="scopeId is required when jobId is set")

    tag = f"[{request.clientCode}]" + (f" [job {request.jobId} scope {request.scopeId}]" if is_job else "")
    response = SyncCalculationResponse(success=False, message="", jobId=request.jobId, scopeId=request.scopeId)

    master_conn_str = os.getenv("MASTER_DB_CONNECTION")
    log_id = None
    started_at_utc = datetime.datetime.utcnow()

    def finish_log(status: str, message: str, rows_processed: int | None = None) -> None:
        # Off the critical path — the response doesn't wait for the master DB.
        if is_job:
            _log_in_background(tag, log_calculation_complete, master_conn_str, request.clientCode,
                               request.entityId, request.versionId, request.yearId, status,
                               f"[job {request.jobId} scope {request.scopeId}] {message}",
                               rows_processed, started_at_utc)
        elif log_id:
            _log_in_background(tag, log_calculation, master_conn_str, request.clientCode,
                               request.entityId, request.versionId, request.yearId, status=status,
                               message=message, rows_processed=rows_processed, started_at=log_id)

    try:
        conn_str = get_connection_string_from_master(request.clientCode)

        # Live mode writes STARTED before touching data, synchronously: a
        # metric change job checks for STARTED rows to wait for calculations
        # already writing live SheetData. Job mode runs under the job's own
        # lock and step log, so it records one row when it finishes.
        if not is_job:
            log_id = log_calculation(
                master_conn_str, request.clientCode,
                request.entityId, request.versionId, request.yearId,
                status='STARTED', message="Sync calculation started")

        if is_job:
            rows = get_staging_data(conn_str, request.jobId, request.scopeId)
        else:
            rows = get_sheet_data(
                conn_str=conn_str,
                entity_id=request.entityId,
                version_id=request.versionId,
                year_id=request.yearId,
                time_period_id=request.timePeriodId)

        if not rows:
            # Job mode: the worker copies every scope's rows into staging
            # before calling — none means something upstream went wrong.
            response.errorKind = "permanent"
            response.message = "No staging rows found for this job scope" if is_job \
                else "No sheet data found for given filters"
            finish_log('FAILED', response.message)
            logger.warning(f"{tag} {response.message}")
            return response

        # The job's NEW time behaviours (live Metrics still has the old ones).
        if request.timeBehaviors:
            for row in rows:
                if row["MetricId"] in request.timeBehaviors:
                    row["TimeBehavior"] = request.timeBehaviors[row["MetricId"]]

        # The job's NEW dependency edges (live MetricDependencies still has
        # the old ones); live mode reads them from the DB as /calculate does.
        if request.dependencyEdges is not None:
            dependency_edges = [(edge[0], edge[1]) for edge in request.dependencyEdges if len(edge) == 2]
        else:
            dependency_edges = get_metric_dependency_edges(conn_str)

        logger.info(f"{tag} Calculating {len(rows)} rows...")
        updates, total_updates, issues = calculate_all(rows, dependency_edges)

        if is_job:
            save_staging_amounts(conn_str, request.jobId, request.scopeId, updates, total_updates)
        else:
            save_calculated_amounts(conn_str, updates, total_updates,
                                    request.entityId, request.versionId, request.yearId, request.timePeriodId)

        response.issues = [
            CalculationIssue(level=i["level"], message=i["message"], metricId=i.get("metric_id"),
                             metricCode=i.get("metric_code"), timeId=i.get("time_id"))
            for i in issues
        ]
        response.issueCount = len(issues)
        response.errorCount = sum(1 for i in issues if i["level"] == "error")
        response.rowsUpdated = len(updates)
        response.success = True
        response.message = f"Calculation complete. {len(updates)} rows updated."
        if issues:
            response.message += f" {len(issues)} issue(s), {response.errorCount} error(s)."
            for issue in issues:
                logger.warning(f"{tag} [{issue['level']}] {issue['message']}")

        finish_log('SUCCESS', response.message, len(updates))
        logger.info(f"{tag} Done in {(datetime.datetime.utcnow() - started_at_utc).total_seconds() * 1000:.0f} ms.")
        return response

    except Exception as e:
        response.success = False
        response.errorKind = classify_error(e)
        response.message = str(e)
        logger.error(f"{tag} {response.errorKind} error: {e}")
        finish_log('FAILED', f"[{response.errorKind}] {e}")
        return response


# Master-DB log writes that the caller shouldn't wait for. Two threads: the
# shared master connection serialises them anyway; this just keeps them off
# the request thread.
_log_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="calc-log")


def _log_in_background(tag: str, fn, *args, **kwargs) -> None:
    def run():
        try:
            fn(*args, **kwargs)
        except Exception as log_error:
            logger.error(f"{tag} Failed to write CalculationLogs: {log_error}")
    _log_executor.submit(run)


async def run_calculation(request: CalculationRequest):
    response = CalculationResponse(
        success=False,
        message="",
        clientCode=request.clientCode,
        entityId=request.entityId,
        versionId=request.versionId,
        yearId=request.yearId,
        timePeriodId=request.timePeriodId,
        productId=request.productId,
        projectId=request.projectId,
        userId=request.userId
    )

    log_id = None
    master_conn_str = os.getenv("MASTER_DB_CONNECTION")

    try:
        logger.info(f"[{request.clientCode}] Fetching connection string...")
        conn_str = get_connection_string_from_master(request.clientCode)

        logger.info(f"[{request.clientCode}] Fetching sheet data...")
        log_id = log_calculation(
            master_conn_str, request.clientCode,
            request.entityId, request.versionId, request.yearId,
            status='STARTED', message='Calculation started'
        )

        rows = get_sheet_data(
            conn_str=conn_str,
            entity_id=request.entityId,
            version_id=request.versionId,
            year_id=request.yearId,
            time_period_id=request.timePeriodId,
            product_id=request.productId,
            project_id=request.projectId
        )

        if not rows:
            response.success = False
            response.message = "No sheet data found for given filters"
            await send_callback(request.callbackUrl, response)
            return

        dependency_edges = get_metric_dependency_edges(conn_str)

        logger.info(f"[{request.clientCode}] Calculating {len(rows)} rows...")
        updates, total_updates, issues = calculate_all(rows, dependency_edges)

        logger.info(f"[{request.clientCode}] Saving {len(updates)} calculated rows...")
        save_calculated_amounts(conn_str, updates, total_updates,
                                request.entityId, request.versionId, request.yearId, request.timePeriodId)

        if issues:
            for issue in issues:
                logger.warning(f"[{request.clientCode}] [{issue['level']}] {issue['message']}")

        status_message = f'{len(updates)} rows updated'
        if issues:
            status_message += f', {len(issues)} issue(s) — see log'

        log_calculation(master_conn_str, request.clientCode,
            request.entityId, request.versionId, request.yearId,
            status='SUCCESS', message=status_message,
            rows_processed=len(updates), started_at=log_id)

        # Calculation still succeeds even with issues — bad cells are
        # defaulted and flagged rather than blocking the rest of the sheet.
        response.success = True
        response.message = f"Calculation complete. {len(updates)} rows updated."
        response.issueCount = len(issues)
        response.issues = [issue["message"] for issue in issues]
        await send_callback(request.callbackUrl, response)
        logger.info(f"[{request.clientCode}] Done.")

    except Exception as e:
        if log_id:
            log_calculation(master_conn_str, request.clientCode,
                request.entityId, request.versionId, request.yearId,
                status='FAILED', message=str(e), started_at=log_id)

        response.success = False
        response.message = str(e)
        await send_callback(request.callbackUrl, response)
        logger.error(f"[{request.clientCode}] Error: {str(e)}")


# FIX 3: Programmatic Uvicorn startup binding to 0.0.0.0
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8002, reload=False)
