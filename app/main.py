import logging
import os
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query

from app.db_init import init as init_db
from app.auth_db_init import init as init_auth_db
from app.routers import instruments, prices, bhavcopy as bhavcopy_router
from app.routers import auth as auth_router, subscriptions as subs_router
from app.cron.fetch_prices import run_all as run_eod_fetch, warm_cache
from app.cron.populate_instruments import run_all as populate_instruments
from app.cron.download_bhavcopy import download_all as download_bhavcopy
from app.cron.sync_bhavcopy import sync_pending as sync_bhavcopy

_logs_dir = Path(os.getenv("LOGS_PATH", "logs"))
_logs_dir.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_logs_dir / "app.log"),
    ],
)
logger = logging.getLogger(__name__)


def _cancel_expired_declined_subscriptions():
    """Run hourly: cancel DECLINED subscriptions whose 24h notice has elapsed."""
    from app.auth_db import AuthSessionLocal
    from sqlalchemy import text as _text
    db = AuthSessionLocal()
    try:
        db.execute(_text("""
            UPDATE subscriptions
            SET status='CANCELLED'
            WHERE status='DECLINED'
              AND cancel_at IS NOT NULL
              AND cancel_at <= datetime('now')
        """))
        db.commit()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup ---
    init_db()
    init_auth_db()
    warm_cache(date.today())

    from apscheduler.schedulers.background import BackgroundScheduler
    _scheduler = BackgroundScheduler()
    _scheduler.add_job(_cancel_expired_declined_subscriptions, "interval", hours=1)
    _scheduler.start()

    logger.info("Server started — all cron jobs disabled, use /admin/* endpoints to trigger manually")
    yield
    # --- Shutdown ---
    _scheduler.shutdown(wait=False)
    logger.info("Server stopped")


app = FastAPI(title="Portfolio Tracker Server", version="0.1.0", lifespan=lifespan)

app.include_router(auth_router.router,      prefix="/auth",           tags=["auth"])
app.include_router(subs_router.router,      prefix="/subscriptions",  tags=["subscriptions"])
app.include_router(instruments.router,      prefix="/instruments",    tags=["instruments"])
app.include_router(prices.router,           prefix="/prices",         tags=["prices"])
app.include_router(bhavcopy_router.router,  prefix="/admin/bhavcopy", tags=["bhavcopy"])


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/admin/fetch-prices")
def admin_fetch_prices(
    trade_date: Optional[str] = Query(None, description="Date YYYY-MM-DD. Defaults to today."),
    force: bool = Query(False, description="Skip trading-day check."),
):
    """Manually trigger the EOD price fetch for a given date."""
    parsed_date = None
    if trade_date:
        try:
            from datetime import datetime
            parsed_date = datetime.strptime(trade_date, "%Y-%m-%d").date()
        except ValueError:
            return {"error": f"Invalid date: {trade_date}. Use YYYY-MM-DD."}
    return run_eod_fetch(parsed_date, force=force)


@app.post("/admin/populate-instruments")
def admin_populate_instruments():
    """Manually trigger instrument master population from NSE/BSE/AMFI. Idempotent."""
    return populate_instruments()


@app.post("/admin/download-bhavcopy")
def admin_download_bhavcopy(
    trade_date: Optional[str] = Query(None, description="Date YYYY-MM-DD. Defaults to today."),
    force: bool = Query(False, description="Re-download even if file already exists."),
):
    """Download NSE/BSE bhavcopy files for a given date."""
    parsed_date = None
    if trade_date:
        try:
            from datetime import datetime as _dt
            parsed_date = _dt.strptime(trade_date, "%Y-%m-%d").date()
        except ValueError:
            return {"error": f"Invalid date: {trade_date}. Use YYYY-MM-DD."}
    return download_bhavcopy(parsed_date, force=force)


@app.post("/admin/sync-bhavcopy")
def admin_sync_bhavcopy():
    """Parse downloaded bhavcopy files and upsert prices into DB."""
    return sync_bhavcopy()


@app.get("/admin/bhavcopy-status")
def admin_bhavcopy_status(
    trade_date: Optional[str] = Query(None, description="Filter by date YYYY-MM-DD."),
    limit: int = Query(50, description="Max rows to return."),
):
    """List bhavcopy_files records — monitor download/sync state."""
    from app.database import engine
    from sqlalchemy import text as _text

    query = "SELECT id, file_name, trade_date, source, status, rows_synced, error, updated_at FROM bhavcopy_files"
    params: dict = {}
    if trade_date:
        query += " WHERE trade_date = :td"
        params["td"] = trade_date
    query += " ORDER BY trade_date DESC, source ASC LIMIT :limit"
    params["limit"] = limit

    with engine.connect() as conn:
        rows = conn.execute(_text(query), params).fetchall()

    return [
        {"id": r[0], "file_name": r[1], "trade_date": r[2],
         "source": r[3], "status": r[4], "rows_synced": r[5],
         "error": r[6], "updated_at": r[7]}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Static file mounts (must be last — API routes take priority)
# ---------------------------------------------------------------------------
from pathlib import Path as _Path
from fastapi.staticfiles import StaticFiles as _StaticFiles

_downloads_dir = _Path("downloads")
_downloads_dir.mkdir(parents=True, exist_ok=True)
app.mount("/downloads", _StaticFiles(directory=str(_downloads_dir)), name="downloads")

_website_dir = _Path("website")
if _website_dir.exists():
    app.mount("/", _StaticFiles(directory=str(_website_dir), html=True), name="website")
