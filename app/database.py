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

    pyodbc_str = "Driver={ODBC Driver 17 for SQL Server};"
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
                   year_id: int,
                   product_id: int = None, project_id: int = None) -> list[dict]:
    """
    Fetch all SheetData rows for given filters from client DB.
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
              AND sd.TimeId BETWEEN 1 AND 12
        """

        params = [entity_id, version_id, year_id]

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


def save_calculated_amounts(conn_str: str, updates: list[dict], total_updates: list[dict]):
    """
    Save calculated Amount values back to SheetData.
    updates = [{ "id": 1, "amount": 5000.00 }, ...]
    """
    try:
        conn = pyodbc.connect(conn_str)
        cursor = conn.cursor()

        for item in updates:
            cursor.execute(
                "UPDATE SheetData SET Amount = ? WHERE Id = ?",
                item["amount"], item["id"]
            )

            # Save TotalAmount — update first row of each metric (TimeId=1)
        for item in total_updates:
            cursor.execute(
                "UPDATE SheetData SET TotalAmount = ? WHERE MetricId = ? AND TimeId = 1",
                item["total_amount"], item["metric_id"]
            )

        conn.commit()
        conn.close()

    except Exception as e:
        raise Exception(f"Client DB save error: {str(e)}")