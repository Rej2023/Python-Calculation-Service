import pyodbc
from typing import Optional
import logging
import threading
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/calculation.log")
    ]
)
logger = logging.getLogger(__name__)


# ── Master DB: one reused connection ─────────────────────────────────────────
# Every calculation used to open 3 fresh connections to the master DB (client
# lookup + 2 log writes) — each a full network connect + login, which was
# ~95% of a calculation's time (the arithmetic itself is a few ms). One
# connection is kept open and shared under a lock (pyodbc connections must
# not be used by two threads at once); a broken one is replaced on next use.
_master_lock = threading.Lock()
_master_conn = None
_master_conn_str = None


def _run_on_master(master_conn_str: str, work):
    """Runs work(cursor) on the shared master connection and commits. Retries
    once on a fresh connection if the kept one has gone stale."""
    global _master_conn, _master_conn_str
    with _master_lock:
        for attempt in (1, 2):
            try:
                if _master_conn is None or _master_conn_str != master_conn_str:
                    if _master_conn is not None:
                        try:
                            _master_conn.close()
                        except pyodbc.Error:
                            pass
                    _master_conn = pyodbc.connect(master_conn_str)
                    _master_conn_str = master_conn_str
                cursor = _master_conn.cursor()
                result = work(cursor)
                _master_conn.commit()
                return result
            except pyodbc.Error:
                try:
                    _master_conn.close()
                except Exception:
                    pass
                _master_conn = None
                if attempt == 2:
                    raise


def log_calculation(master_conn_str: str, client_code: str, entity_id: int,
                    version_id: int, year_id: int, status: str,
                    message: str = None, rows_processed: int = None,
                    started_at=None) -> int:
    """Insert or update calculation log. Returns log Id."""
    import datetime

    def work(cursor):
        if status == 'STARTED':
            # OUTPUT returns the new Id in the same round trip (was a second
            # SELECT @@IDENTITY query).
            cursor.execute("""
                INSERT INTO CalculationLogs
                    (ClientCode, EntityId, VersionId, YearId, Status, Message, StartedAt, CreatedAt)
                OUTPUT INSERTED.Id
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, client_code, entity_id, version_id, year_id, status,
                message, datetime.datetime.utcnow(), datetime.datetime.utcnow())
            return int(cursor.fetchone()[0])

        cursor.execute("""
            UPDATE CalculationLogs
            SET Status = ?, Message = ?, RowsProcessed = ?, CompletedAt = ?
            WHERE Id = ?
        """, status, message, rows_processed, datetime.datetime.utcnow(), started_at)
        return started_at  # reuse as id

    return _run_on_master(master_conn_str, work)


def log_calculation_complete(master_conn_str: str, client_code: str, entity_id: int,
                             version_id: int, year_id: int, status: str, message: str,
                             rows_processed: int, started_at_utc) -> None:
    """One finished log row (start + outcome) in a single write — for runs
    that don't need a STARTED row up front (metric change jobs log every step
    in their own job log)."""
    import datetime

    def work(cursor):
        cursor.execute("""
            INSERT INTO CalculationLogs
                (ClientCode, EntityId, VersionId, YearId, Status, Message, RowsProcessed, StartedAt, CompletedAt, CreatedAt)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, client_code, entity_id, version_id, year_id, status, message, rows_processed,
            started_at_utc, datetime.datetime.utcnow(), started_at_utc)

    _run_on_master(master_conn_str, work)



def convert_to_pyodbc_conn_str(ado_conn_str: str) -> str:
    """
    Convert ADO.NET connection string to pyodbc format.
    """
    import re
    mapping = {
        "Server": "Server",
        "Database": "Database",
        "User ID": "UID",
        "Password": "PWD",
    }

    params = dict(part.strip().split("=", 1) for part in ado_conn_str.split(";") if "=" in part)

    pyodbc_str = "Driver={ODBC Driver 17 for SQL Server};"
    pyodbc_str += f"Server={params.get('Server', '')};"
    pyodbc_str += f"Database={params.get('Database', '')};"

    # Windows authentication (e.g. a local test database) — no user/password.
    trusted = params.get("Trusted_Connection", params.get("Integrated Security", "")).strip().lower()
    if trusted in ("true", "yes", "sspi"):
        pyodbc_str += "Trusted_Connection=yes;"
    else:
        pyodbc_str += f"UID={params.get('User ID', '')};"
        pyodbc_str += f"PWD={params.get('Password', '')};"
    pyodbc_str += "TrustServerCertificate=yes;"

    return pyodbc_str

# Client connection strings change almost never — cache each for a few
# minutes instead of asking the master DB on every calculation.
_CLIENT_CONN_TTL_SECONDS = 300
_client_conn_cache: dict[str, tuple[str, float]] = {}
_client_conn_lock = threading.Lock()


def get_connection_string_from_master(client_code: str) -> Optional[str]:
    """
    Connect to master DB and fetch connection string by client code.
    Master DB connection is read from environment variable.
    Cached per client for _CLIENT_CONN_TTL_SECONDS.
    """
    with _client_conn_lock:
        cached = _client_conn_cache.get(client_code)
        if cached and cached[1] > time.monotonic():
            return cached[0]

    conn_str = _resolve_connection_string(client_code)
    with _client_conn_lock:
        _client_conn_cache[client_code] = (conn_str, time.monotonic() + _CLIENT_CONN_TTL_SECONDS)
    return conn_str


def _resolve_connection_string(client_code: str) -> str:
    import os
    from dotenv import load_dotenv
    load_dotenv()

    master_conn_str = os.getenv("MASTER_DB_CONNECTION")

    try:
        def work(cursor):
            cursor.execute(
                "SELECT ConnectionString FROM ClientInformation WHERE ClientCode = ?",
                client_code
            )
            return cursor.fetchone()

        row = _run_on_master(master_conn_str, work)

        if row is None:
            raise ValueError(f"No client found with code: {client_code}")

        # Local testing only: CLIENT_DB_OVERRIDE_<CLIENTCODE> (set in the
        # untracked .env.local) points this client at a local copy of its
        # database while the master DB stays shared. Same ADO.NET format as
        # ClientInformation.ConnectionString (Server=…;Database=…;User ID=…;Password=…).
        local_override = os.getenv(f"CLIENT_DB_OVERRIDE_{client_code.upper()}")
        if local_override:
            logger.info(f"[{client_code}] Using LOCAL database override (testing only).")
            return convert_to_pyodbc_conn_str(local_override)

        # Never log the connection string itself — it contains the password.
        logger.info(f"[{client_code}] Client connection resolved from master DB.")
        return convert_to_pyodbc_conn_str(row[0])

    except Exception as e:
        raise Exception(f"Master DB error: {str(e)}") from e


def get_sheet_data(conn_str: str, entity_id: int, version_id: int,
                   year_id: int, time_period_id: int,
                   product_id: int = None, project_id: int = None) -> list[dict]:
    """
    Fetch all SheetData rows for given filters from client DB.

    Scoped by TimePeriodId, not a fixed TimeId range — a bare TimeId means a
    different thing per template (CALENDAR/FINANCIAL top out at 12,
    PERIODS_13 goes to 13), so the old "BETWEEN 1 AND 12" bound both silently
    dropped period 13 and couldn't disambiguate two templates' data anyway.

    product_id/project_id are accepted for call-signature compatibility but
    deliberately unused: a Driver metric can now have several product/project
    CHILD rows (ProductId or ProjectId set) alongside its own aggregate row
    (both null). The C# save path always sums children into that aggregate
    row before this service ever runs, so calculation must only ever read
    the aggregate row — reading a child row here (via an override) would
    double-count against the already-summed parent.
    """
    try:
        conn = pyodbc.connect(conn_str)
        cursor = conn.cursor()

        query = """
            SELECT
                sd.Id,
                sd.MetricId,
                sd.TimeId,
                sd.Amount,
                sd.Formula,
                sd.Type,
                m.Code AS MetricCode,
                m.Name AS MetricName,
                m.TimeBehavior,
                m.TotalMethod
            FROM SheetData sd
            INNER JOIN Metrics m ON sd.MetricId = m.Id
            WHERE sd.EntityId = ?
              AND sd.VersionId = ?
              AND sd.YearId = ?
              AND sd.TimePeriodId = ?
              AND sd.ProductId IS NULL
              AND sd.ProjectId IS NULL
        """

        params = [entity_id, version_id, year_id, time_period_id]

        cursor.execute(query, params)
        columns = [column[0] for column in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]

        conn.close()
        logger.info(f"Sheet data fetched: {len(rows)} rows")
        return rows

    except Exception as e:
        raise Exception(f"Client DB fetch error: {str(e)}") from e


def get_metric_dependency_edges(conn_str: str) -> list[tuple[str, str]]:
    """
    Fetch persisted formula-dependency edges as (depends_on_code, metric_code)
    pairs — the same MetricDependencies table MetricRepository (C#) maintains
    and cycle-checks at formula-save-time. Replaces the old approach of
    regex-deriving dependencies here on every calculation.
    """
    try:
        conn = pyodbc.connect(conn_str)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT dm.Code AS DependsOnCode, m.Code AS MetricCode
            FROM MetricDependencies md
            INNER JOIN Metrics m ON md.MetricId = m.Id
            INNER JOIN Metrics dm ON md.DependsOnMetricId = dm.Id
        """)
        edges = [(row[0], row[1]) for row in cursor.fetchall()]
        conn.close()
        logger.info(f"Dependency edges fetched: {len(edges)}")
        return edges

    except Exception as e:
        raise Exception(f"Client DB dependency fetch error: {str(e)}") from e


def save_calculated_amounts(conn_str: str, updates: list[dict], total_updates: list[dict],
                            entity_id: int, version_id: int, year_id: int, time_period_id: int):
    """
    Save calculated Amount values back to SheetData.
    updates = [{ "id": 1, "amount": 5000.00 }, ...]
    """
    from decimal import Decimal
    try:
        conn = pyodbc.connect(conn_str)
        cursor = conn.cursor()
        # Batches all rows into a single round-trip per statement shape instead
        # of one execute() per row (a 30-metric x 12-period sheet is 360+ rows)
        # — the dominant cost of this function before was per-row round-trip
        # latency, not the database work itself.
        cursor.fast_executemany = True

        cursor.setinputsizes([(pyodbc.SQL_DECIMAL, 38, 10), pyodbc.SQL_INTEGER])

        if updates:
            cursor.setinputsizes([(pyodbc.SQL_DECIMAL, 38, 10), pyodbc.SQL_INTEGER, pyodbc.SQL_INTEGER])
            cursor.executemany(
                "UPDATE SheetData SET Amount = ? WHERE Id = ?",
                [(Decimal(str(item["amount"])) if item["amount"] is not None else None, item["id"]) for item in updates]
            )

        # Save TotalAmount — update first row of each metric (TimeId=1). That
        # row always exists for every template (CALENDAR/FINANCIAL/PERIODS_12/13
        # all include the real Jan/period-1 row), so this convention still
        # holds; TimePeriodId is added so it can't touch a leftover row from a
        # different (deleted) template sharing the same MetricId/TimeId=1.
        # ProductId/ProjectId IS NULL is required now too — without it, a
        # Driver-with-children metric's product/project CHILD rows also match
        # MetricId+TimeId=1+TimePeriodId and would all get silently overwritten
        # with the parent's TotalAmount in the same statement.
        # EntityId/VersionId/YearId are required too — without them this one
        # sheet's totals overwrote the same metric's TotalAmount on EVERY
        # entity/version/year sharing the TimePeriod.
        if total_updates:
            cursor.setinputsizes([(pyodbc.SQL_DECIMAL, 38, 10)] + [pyodbc.SQL_INTEGER] * 5)
            cursor.executemany(
                """
                UPDATE SheetData
                SET TotalAmount = ?
                WHERE MetricId = ? AND TimeId = 1 AND TimePeriodId = ?
                  AND EntityId = ? AND VersionId = ? AND YearId = ?
                  AND ProductId IS NULL AND ProjectId IS NULL
                """,
                [(Decimal(str(item["total_amount"])) if item["total_amount"] is not None else None,
                  item["metric_id"], time_period_id, entity_id, version_id, year_id) for item in total_updates]
            )

        conn.commit()
        conn.close()

    except Exception as e:
        raise Exception(f"Client DB save error: {str(e)}") from e


# ── Metric change jobs: staging read/write ──────────────────────────────────
# A MetricChangeJob recalculates into SheetDataStaging (one copy of each live
# SheetData row per job scope, with the job's NEW formula already written onto
# it by the .NET worker). Live SheetData is never read or written here — the
# worker swaps staging into live in one transaction once every scope passes.
# These deliberately let pyodbc errors propagate unwrapped, so the endpoint can
# tell a transient DB failure (retry) from a permanent one.

def get_staging_data(conn_str: str, job_id: int, scope_id: int) -> list[dict]:
    """
    Same row shape as get_sheet_data, read from the job's staging rows for one
    scope. Id is the STAGING row id, so save_staging_amounts writes back to
    staging. Aggregate rows only (ProductId/ProjectId NULL) — same reason as
    get_sheet_data: children are already summed into the aggregate row.
    """
    conn = pyodbc.connect(conn_str)
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT
                s.Id,
                s.MetricId,
                s.TimeId,
                s.Amount,
                s.Formula,
                s.Type,
                m.Code AS MetricCode,
                m.Name AS MetricName,
                m.TimeBehavior,
                m.TotalMethod
            FROM SheetDataStaging s
            INNER JOIN Metrics m ON s.MetricId = m.Id
            WHERE s.JobId = ?
              AND s.ScopeId = ?
              AND s.ProductId IS NULL
              AND s.ProjectId IS NULL
        """, job_id, scope_id)
        columns = [column[0] for column in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        logger.info(f"[job {job_id} scope {scope_id}] Staging data fetched: {len(rows)} rows")
        return rows
    finally:
        conn.close()


def save_staging_amounts(conn_str: str, job_id: int, scope_id: int,
                         updates: list[dict], total_updates: list[dict]) -> None:
    """
    Writes calculated Amount / TotalAmount back to the job's staging rows for
    one scope, in a single transaction — a failure part-way leaves nothing
    half-written. Safe to re-run (a retry just overwrites the same rows with
    the same results), so the worker never has to clean up before retrying.
    """
    from decimal import Decimal

    conn = pyodbc.connect(conn_str)
    try:
        conn.autocommit = False
        cursor = conn.cursor()
        cursor.fast_executemany = True

        if updates:
            cursor.setinputsizes([(pyodbc.SQL_DECIMAL, 38, 10), pyodbc.SQL_INTEGER, pyodbc.SQL_INTEGER, pyodbc.SQL_INTEGER])
            cursor.executemany(
                "UPDATE SheetDataStaging SET Amount = ? WHERE Id = ? AND JobId = ? AND ScopeId = ?",
                [(Decimal(str(item["amount"])) if item["amount"] is not None else None,
                  item["id"], job_id, scope_id) for item in updates]
            )

        # Same TimeId=1 convention as save_calculated_amounts, scoped to this
        # job's scope only.
        if total_updates:
            cursor.setinputsizes([(pyodbc.SQL_DECIMAL, 38, 10)] + [pyodbc.SQL_INTEGER] * 3)
            cursor.executemany(
                """
                UPDATE SheetDataStaging
                SET TotalAmount = ?
                WHERE MetricId = ? AND JobId = ? AND ScopeId = ? AND TimeId = 1
                  AND ProductId IS NULL AND ProjectId IS NULL
                """,
                [(Decimal(str(item["total_amount"])) if item["total_amount"] is not None else None,
                  item["metric_id"], job_id, scope_id) for item in total_updates]
            )

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()