import os
import logging
from pathlib import Path
from dotenv import load_dotenv

# FIX 1: Force absolute path for the .env file based on this script's location
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(dotenv_path=BASE_DIR / ".env")

from fastapi import FastAPI, BackgroundTasks, HTTPException, Header
from app.models import CalculationRequest, CalculationResponse
from app.database import get_connection_string_from_master, get_sheet_data, get_metric_dependency_edges, save_calculated_amounts, log_calculation
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
    if x_api_key != os.getenv("API_KEY"):
        logger.warning(f"Unauthorized access attempt. Expected: {os.getenv('API_KEY')}, Got: {x_api_key}")
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    background_tasks.add_task(run_calculation, request)
    return {"status": "accepted", "message": "Calculation started"}


async def run_calculation(request: CalculationRequest):
    response = CalculationResponse(
        success=False,
        message="",
        clientCode=request.clientCode,
        entityId=request.entityId,
        versionId=request.versionId,
        yearId=request.yearId
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
        save_calculated_amounts(conn_str, updates, total_updates)

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
