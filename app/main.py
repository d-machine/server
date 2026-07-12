import logging
import os
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.db_init import init as init_db
from app.auth_db_init import init as init_auth_db
from app.routers import instruments, prices, bhavcopy as bhavcopy_router
from app.routers import auth as auth_router, subscriptions as subs_router, persons as persons_router, tickets as tickets_router
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


def _cancel_expired_subscriptions():
    """Run hourly: cancel subscriptions whose expires_at has passed."""
    from app.auth_db import AuthSessionLocal
    from sqlalchemy import text as _text
    db = AuthSessionLocal()
    try:
        db.execute(_text("""
            UPDATE subscriptions
            SET status='EXPIRED'
            WHERE status='ACTIVE'
              AND expires_at IS NOT NULL
              AND expires_at <= datetime('now')
        """))
        db.commit()
    finally:
        db.close()


def _send_underpaid_reminders():
    """Run daily at 09:00 IST: email underpaid users with ≤7 days left before lock."""
    import smtplib
    from email.mime.text import MIMEText
    from datetime import timedelta

    smtp_pass = os.getenv("SMTP_PASS", "")
    if not smtp_pass:
        return

    from app.auth_db import AuthSessionLocal
    from sqlalchemy import text as _text
    db = AuthSessionLocal()
    try:
        rows = db.execute(_text("""
            SELECT u.person_id, u.required_price, u.underpaid_since,
                   u.last_reminder_at, usr.email
            FROM underpaid_users u
            JOIN persons p ON p.person_id = u.person_id
            JOIN users usr ON usr.user_id = p.user_id
            WHERE date(u.underpaid_since, '+23 days') <= date('now')
              AND date(u.underpaid_since, '+30 days') >= date('now')
              AND (u.last_reminder_at IS NULL
                   OR datetime(u.last_reminder_at, '+24 hours') <= datetime('now'))
        """)).fetchall()

        smtp_host  = os.getenv("SMTP_HOST", "smtp.gmail.com")
        smtp_port  = int(os.getenv("SMTP_PORT", 587))
        smtp_user  = os.getenv("SMTP_USER", "sumitshark13@gmail.com")
        from_email = os.getenv("FROM_EMAIL", "sumitshark13@gmail.com")
        base_url   = os.getenv("BASE_URL", "https://arthdeskapi.ashokitservices.com")

        for r in rows:
            person_id, required_price, underpaid_since, _, email = r
            from datetime import datetime as _dt
            lock_date = (_dt.strptime(underpaid_since, "%Y-%m-%d") + timedelta(days=30)).strftime("%d %b %Y")

            msg = MIMEText(
                f"Hi,\n\n"
                f"This is a reminder that your ArthaDesk subscription needs to be upgraded.\n"
                f"Your required plan price is ₹{required_price:,}/year.\n\n"
                f"Your Capital Gains and Tax features will be locked on {lock_date}.\n\n"
                f"Upgrade here: {base_url}/subscribe.html\n\n"
                f"— ArthaDesk Team",
                "plain",
            )
            msg["Subject"] = f"Reminder: ArthaDesk subscription upgrade due {lock_date}"
            msg["From"]    = from_email
            msg["To"]      = email

            try:
                with smtplib.SMTP(smtp_host, smtp_port) as s:
                    s.starttls()
                    s.login(smtp_user, smtp_pass)
                    s.send_message(msg)
                db.execute(
                    _text("UPDATE underpaid_users SET last_reminder_at=datetime('now') WHERE person_id=:pid"),
                    {"pid": person_id},
                )
                db.commit()
            except Exception:
                pass
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
    _scheduler.add_job(_cancel_expired_subscriptions, "interval", hours=1)
    # 09:00 IST = 03:30 UTC
    _scheduler.add_job(_send_underpaid_reminders, "cron", hour=3, minute=30)
    _scheduler.start()

    logger.info("Server started — all cron jobs disabled, use /admin/* endpoints to trigger manually")
    yield
    # --- Shutdown ---
    _scheduler.shutdown(wait=False)
    logger.info("Server stopped")


app = FastAPI(title="Portfolio Tracker Server", version="0.1.0", lifespan=lifespan)

_allowed_origins = os.getenv(
    "CORS_ORIGINS",
    "http://localhost:8080,http://localhost:8081,https://arthdesk.ashokitservices.com,https://arthdeskadmin.ashokitservices.com",
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router.router,      prefix="/auth",           tags=["auth"])
app.include_router(subs_router.router,      prefix="/subscriptions",  tags=["subscriptions"])
app.include_router(tickets_router.router,   prefix="/tickets",        tags=["tickets"])
app.include_router(persons_router.router,   prefix="/persons",        tags=["persons"])
app.include_router(instruments.router,      prefix="/instruments",    tags=["instruments"])
app.include_router(prices.router,           prefix="/prices",         tags=["prices"])
app.include_router(bhavcopy_router.router,  prefix="/admin/bhavcopy", tags=["bhavcopy"])


_LOCAL_DEV = os.getenv("LOCAL_DEV", "").lower() in ("1", "true", "yes")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/screenshots/{filename}")
def serve_screenshot(filename: str):
    """Serve uploaded screenshots locally, mimicking GCS signed URLs for local dev."""
    if not _LOCAL_DEV:
        raise HTTPException(status_code=404)
    path = Path(f"/app/data/screenshots/{filename}")
    if not path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(path)


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


