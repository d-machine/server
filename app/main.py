import logging
import threading
from contextlib import asynccontextmanager
from datetime import date
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI, Query

from app.db_init import init as init_db
from app.routers import instruments, prices
from app.cron.fetch_prices import run_all as run_eod_fetch, warm_cache, intraday_fetch_job
from app.cron.populate_instruments import run_all as populate_instruments

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _startup_populate_and_fetch():
    """
    Runs in a background thread after startup so it doesn't block the server.
    1. Populates instrument masters from NSE/BSE/AMFI (idempotent upserts).
    2. Triggers an EOD price fetch for today (force=True in case it's a holiday).
    """
    logger.info("[STARTUP] Running instrument population + initial price fetch")
    try:
        populate_instruments()
    except Exception as e:
        logger.error(f"[STARTUP] populate_instruments failed: {e}", exc_info=True)

    try:
        run_eod_fetch(force=True)
    except Exception as e:
        logger.error(f"[STARTUP] initial price fetch failed: {e}", exc_info=True)

    logger.info("[STARTUP] Background startup tasks complete")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup ---
    init_db()           # create tables if not exists
    warm_cache(date.today())    # warm from whatever prices are already in DB

    # Instrument population + price fetch in background — don't block server start
    threading.Thread(target=_startup_populate_and_fetch, daemon=True).start()

    scheduler = BackgroundScheduler(timezone="Asia/Kolkata")

    # EOD price fetch at 18:30 IST every weekday
    scheduler.add_job(
        run_eod_fetch,
        CronTrigger(day_of_week="mon-fri", hour=18, minute=30, timezone="Asia/Kolkata"),
        id="eod_fetch",
        name="EOD Price Fetch",
        misfire_grace_time=3600,
    )

    # Intraday price sync every 30 min (guard inside job: 09:35–15:35 IST weekdays)
    scheduler.add_job(
        intraday_fetch_job,
        IntervalTrigger(minutes=30, timezone="Asia/Kolkata"),
        id="intraday_fetch",
        name="Intraday Price Sync",
    )

    # Weekly instrument master refresh every Sunday at 02:00 IST
    scheduler.add_job(
        populate_instruments,
        CronTrigger(day_of_week="sun", hour=2, timezone="Asia/Kolkata"),
        id="populate_instruments",
        name="Instrument Master Refresh",
    )

    scheduler.start()
    logger.info("APScheduler started — EOD fetch at 18:30 IST weekdays, "
                "instrument refresh every Sunday 02:00 IST")

    yield

    # --- Shutdown ---
    scheduler.shutdown(wait=False)
    logger.info("APScheduler stopped")


app = FastAPI(title="Portfolio Tracker Server", version="0.1.0", lifespan=lifespan)

app.include_router(instruments.router, prefix="/instruments", tags=["instruments"])
app.include_router(prices.router,      prefix="/prices",      tags=["prices"])


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/admin/fetch-prices")
def admin_fetch_prices(
    trade_date: Optional[str] = Query(
        None,
        description="Date in YYYY-MM-DD. Defaults to today.",
    ),
    force: bool = Query(
        False,
        description="Skip trading-day check. Use for backfills or holidays.",
    ),
):
    """Manually trigger the EOD price fetch for a given date."""
    parsed_date = None
    if trade_date:
        try:
            from datetime import datetime
            parsed_date = datetime.strptime(trade_date, "%Y-%m-%d").date()
        except ValueError:
            return {"error": f"Invalid date: {trade_date}. Use YYYY-MM-DD."}

    result = run_eod_fetch(parsed_date, force=force)
    return result


@app.post("/admin/populate-instruments")
def admin_populate_instruments():
    """
    Manually trigger instrument master population from NSE/BSE/AMFI.
    Safe to call multiple times — all upserts are idempotent.
    """
    result = populate_instruments()
    return result
