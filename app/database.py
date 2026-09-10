import pyodbc
from typing import Optional
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/calculation.log")
    ]
)
logger = logging.getLogger(__name__)


def log_calculation(master_conn_str: str, client_code: str, entity_id: int, 
                    version_id: int, year_id: int, status: str, 
                    message: str = None, rows_processed: int = None,
                    started_at=None) -> int:
    """Insert or update calculation log. Returns log Id."""
    import datetime
    conn = pyodbc.connect(master_conn_str)
    cursor = conn.cursor()
    
    if status == 'STARTED':
        cursor.execute("""
            INSERT INTO CalculationLogs 
                (ClientCode, EntityId, VersionId, YearId, Status, Message, StartedAt, CreatedAt)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, client_code, entity_id, version_id, year_id, status, 
            message, datetime.datetime.utcnow(), datetime.datetime.utcnow())
        cursor.execute("SELECT @@IDENTITY")
        log_id = int(cursor.fetchone()[0])
    else:
        cursor.execute("""
            UPDATE CalculationLogs 
            SET Status = ?, Message = ?, RowsProcessed = ?, CompletedAt = ?
            WHERE Id = ?
        """, status, message, rows_processed, datetime.datetime.utcnow(), started_at)
        log_id = started_at  # reuse as id
    
    conn.commit()
    conn.close()
    return log_id



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

    pyodbc_str = "Driver={ODBC Driver 18 for SQL Server};"
    pyodbc_str += f"Server={params.get('Server', '')};"
    pyodbc_str += f"Database={params.get('Database', '')};"
    pyodbc_str += f"UID={params.get('User ID', '')};"
    pyodbc_str += f"PWD={params.get('Password', '')};"
    pyodbc_str += "TrustServerCertificate=yes;"

    return pyodbc_str

def get_connection_string_from_master(client_code: str) -> Optional[str]:
    """
    Connect to master DB and fetch connection string by client code.
    Master DB connection is read from environment variable.
    """
    import os
    from dotenv import load_dotenv
    load_dotenv()

    master_conn_str = os.getenv("MASTER_DB_CONNECTION")

    try:
        conn = pyodbc.connect(master_conn_str)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT ConnectionString FROM ClientInformation WHERE ClientCode = ?",
            client_code
        )
        row = cursor.fetchone()
        conn.close()

        

        if row is None:
            raise ValueError(f"No client found with code: {client_code}")

        logger.info(f"[{row[0]}] Fetching master DB...")
        return convert_to_pyodbc_conn_str(row[0]);

    except Exception as e:
        raise Exception(f"Master DB error: {str(e)}")


def get_sheet_data(conn_str: str, entity_id: int, version_id: int,
                   year_id: int, time_period_id: int,
                   product_id: int = None, project_id: int = None) -> list[dict]:
    """
    Fetch all SheetData rows for given filters from client DB.

    Scoped by TimePeriodId, not a fixed TimeId range — a bare TimeId means a
    different thing per template (CALENDAR/FINANCIAL top out at 12,
    PERIODS_13 goes to 13), so the old "BETWEEN 1 AND 12" bound both silently
    dropped period 13 and couldn't disambiguate two templates' data anyway.
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
        """

        params = [entity_id, version_id, year_id, time_period_id]

        if product_id is not None and product_id != 0:
            query += " AND sd.ProductId = ?"
            params.append(product_id)

        if project_id is not None and project_id != 0:
             query += " AND sd.ProjectId = ?"
             params.append(project_id)

        cursor.execute(query, params)
        columns = [column[0] for column in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]

        conn.close()
        logger.info(f"Sheet data fetched: {len(rows)} rows")
        return rows

    except Exception as e:
        raise Exception(f"Client DB fetch error: {str(e)}")


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
        raise Exception(f"Client DB dependency fetch error: {str(e)}")


def save_calculated_amounts(conn_str: str, updates: list[dict], total_updates: list[dict], time_period_id: int):
    """
    Save calculated Amount values back to SheetData.
    updates = [{ "id": 1, "amount": 5000.00 }, ...]
    """
    try:
        conn = pyodbc.connect(conn_str)
        cursor = conn.cursor()
        # Batches all rows into a single round-trip per statement shape instead
        # of one execute() per row (a 30-metric x 12-period sheet is 360+ rows)
        # — the dominant cost of this function before was per-row round-trip
        # latency, not the database work itself.
        cursor.fast_executemany = True

        if updates:
            cursor.executemany(
                "UPDATE SheetData SET Amount = ? WHERE Id = ?",
                [(item["amount"], item["id"]) for item in updates]
            )

        # Save TotalAmount — update first row of each metric (TimeId=1). That
        # row always exists for every template (CALENDAR/FINANCIAL/PERIODS_12/13
        # all include the real Jan/period-1 row), so this convention still
        # holds; TimePeriodId is added so it can't touch a leftover row from a
        # different (deleted) template sharing the same MetricId/TimeId=1.
        if total_updates:
            cursor.executemany(
                "UPDATE SheetData SET TotalAmount = ? WHERE MetricId = ? AND TimeId = 1 AND TimePeriodId = ?",
                [(item["total_amount"], item["metric_id"], time_period_id) for item in total_updates]
            )

        conn.commit()
        conn.close()

    except Exception as e:
        raise Exception(f"Client DB save error: {str(e)}")