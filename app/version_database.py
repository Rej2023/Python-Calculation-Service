"""
Client-DB access for the version copy/delete calculation (VersionJobSheetStaging).

Separate from app/database.py on purpose — nothing here is shared with, or changes,
the live-sheet or metric-change code paths. pyodbc errors are deliberately NOT wrapped,
so the endpoint can tell a transient DB failure (retry) from a permanent one.
"""
import logging
from decimal import Decimal

import pyodbc

logger = logging.getLogger(__name__)


def get_version_staging_rows(conn_str: str, job_id: int, scope_id: int) -> list[dict]:
    """
    Same row shape as database.get_sheet_data, read from the job's staging rows for one
    scope. Id is the STAGING row id, so the save below writes back to staging. Aggregate
    rows only (ProductId/ProjectId NULL): product/project children are summed into the
    aggregate row before staging, and calculation must never read a child row.
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
            FROM VersionJobSheetStaging s
            INNER JOIN Metrics m ON s.MetricId = m.Id
            WHERE s.JobId = ?
              AND s.ScopeId = ?
              AND s.ProductId IS NULL
              AND s.ProjectId IS NULL
        """, job_id, scope_id)
        columns = [column[0] for column in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        logger.info(f"[version job {job_id} scope {scope_id}] Staging rows fetched: {len(rows)}")
        return rows
    finally:
        conn.close()


def save_version_staging_amounts(conn_str: str, job_id: int, scope_id: int,
                                 updates: list[dict], total_updates: list[dict]) -> int:
    """
    Writes calculated Amount / TotalAmount back to the job's staging rows for one scope,
    in a single transaction — a failure part-way leaves nothing half-written. Safe to
    re-run (a retry clears the scope's totals first, then rewrites everything), so the
    worker never has to clean up before retrying.

    TotalAmount lives on ONE row per metric (the readers take the single non-null one):
    the TimeId = 1 row when the sheet has it (same convention as a normal save), otherwise
    the lowest TimeId of the sheet — a partial copy / delete may not contain period 1.

    Returns the number of metrics that ended up with a total.
    """
    conn = pyodbc.connect(conn_str)
    try:
        conn.autocommit = False

        # Idempotent re-run: forget any totals a previous attempt wrote for this scope.
        clear = conn.cursor()
        clear.execute(
            "UPDATE VersionJobSheetStaging SET TotalAmount = NULL WHERE JobId = ? AND ScopeId = ?",
            job_id, scope_id)

        if updates:
            amounts = conn.cursor()
            amounts.fast_executemany = True
            amounts.setinputsizes([(pyodbc.SQL_DECIMAL, 38, 10), pyodbc.SQL_INTEGER,
                                   pyodbc.SQL_INTEGER, pyodbc.SQL_INTEGER])
            amounts.executemany(
                "UPDATE VersionJobSheetStaging SET Amount = ? WHERE Id = ? AND JobId = ? AND ScopeId = ?",
                [(Decimal(str(item["amount"])) if item["amount"] is not None else None,
                  item["id"], job_id, scope_id) for item in updates]
            )

        if total_updates:
            totals = conn.cursor()
            totals.executemany(
                """
                UPDATE t
                SET t.TotalAmount = ?
                FROM VersionJobSheetStaging t
                WHERE t.JobId = ? AND t.ScopeId = ? AND t.MetricId = ?
                  AND t.ProductId IS NULL AND t.ProjectId IS NULL
                  AND t.TimeId = (
                      SELECT CASE WHEN SUM(CASE WHEN x.TimeId = 1 THEN 1 ELSE 0 END) > 0
                                  THEN 1 ELSE MIN(x.TimeId) END
                      FROM VersionJobSheetStaging x
                      WHERE x.JobId = t.JobId AND x.ScopeId = t.ScopeId AND x.MetricId = t.MetricId
                        AND x.ProductId IS NULL AND x.ProjectId IS NULL)
                """,
                [(Decimal(str(item["total_amount"])) if item["total_amount"] is not None else None,
                  job_id, scope_id, item["metric_id"]) for item in total_updates]
            )

        check = conn.cursor()
        check.execute("""
            SELECT COUNT(*) FROM (
                SELECT MetricId
                FROM VersionJobSheetStaging
                WHERE JobId = ? AND ScopeId = ? AND ProductId IS NULL AND ProjectId IS NULL
                GROUP BY MetricId
                HAVING SUM(CASE WHEN TotalAmount IS NOT NULL THEN 1 ELSE 0 END) = 1
            ) q
        """, job_id, scope_id)
        metrics_with_total = int(check.fetchone()[0])

        conn.commit()
        return metrics_with_total
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
