"""
Bhavcopy admin endpoints.

GET  /admin/bhavcopy/download    - Bulk historical download for a source + date range (background)
POST /admin/bhavcopy/sync-inbox  - Register original NSE/BSE files dropped in /app/inbox/
POST /admin/bhavcopy/parse       - Parse all downloaded files for a source into DB tables
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.cron.bhavcopy import nse_eq, nse_fo, bse_eq, bse_fo, amfi, mcx
from app.cron.bhavcopy.common import date_dir
from app.cron.bhavcopy.constants import FileStatus
from app.cron.bhavcopy.register_file import register as register_file

logger = logging.getLogger(__name__)

router = APIRouter()

ERRORS_DIR = Path(os.getenv("ERRORS_PATH", "errors")) / "bhavcopy"
INBOX_DIR  = Path(os.getenv("INBOX_PATH",  "inbox"))

_INBOX_SOURCES = [
    (re.compile(r"BhavCopy_NSE_CM_0_0_0_(\d{8})_F_0000\.csv(\.zip)?$", re.IGNORECASE),
     "NSE_EQ", "BhavCopy_NSE_CM_0_0_0_{}_F_0000.csv"),
    (re.compile(r"BhavCopy_NSE_FO_0_0_0_(\d{8})_F_0000\.csv(\.zip)?$", re.IGNORECASE),
     "NSE_FO", "BhavCopy_NSE_FO_0_0_0_{}_F_0000.csv"),
    (re.compile(r"BhavCopy_BSE_CM_0_0_0_(\d{8})_F_0000\.csv$", re.IGNORECASE),
     "BSE_EQ", "BhavCopy_BSE_CM_0_0_0_{}_F_0000.CSV"),
    (re.compile(r"BhavCopy_BSE_FO_0_0_0_(\d{8})_F_0000\.csv$", re.IGNORECASE),
     "BSE_FO", "BhavCopy_BSE_FO_0_0_0_{}_F_0000.CSV"),
]

SOURCES = {
    "NSE_EQ":   nse_eq.download,
    "NSE_FO":   nse_fo.download,
    "BSE_EQ":   bse_eq.download,
    "BSE_FO":   bse_fo.download,
    "AMFI_NAV": amfi.download,
    "MCX":      mcx.download,
}

MIN_SLEEP_SECONDS = 30


# -- Enums & Models -----------------------------------------------------------

class BhavSource(str, Enum):
    NSE_EQ   = "NSE_EQ"
    NSE_FO   = "NSE_FO"
    BSE_EQ   = "BSE_EQ"
    BSE_FO   = "BSE_FO"
    AMFI_NAV = "AMFI_NAV"
    MCX      = "MCX"


class DownloadJobResponse(BaseModel):
    status:        str
    job_id:        str
    source:        str
    start_date:    str
    end_date:      str
    trading_days:  int
    sleep_seconds: int
    force:         bool
    error_file:    str


class SyncInboxResponse(BaseModel):
    synced:    list[str]
    can_force: list[str]
    blocked:   list[str]
    invalid:   list[str]


class ParseJobResponse(BaseModel):
    status:      str
    job_id:      str
    source:      str
    force:       bool
    result_file: str


class ParseResponse(BaseModel):
    source:            str
    files_synced:      int
    files_failed:      int
    total_rows_synced: int
    errors:            list[dict]


# -- Helpers ------------------------------------------------------------------

def _trading_days(start: date, end: date) -> list[date]:
    dates, current = [], start
    while current <= end:
        dates.append(current)
        current += timedelta(days=1)
    return dates


def _error_file_path(job_id: str) -> Path:
    ERRORS_DIR.mkdir(parents=True, exist_ok=True)
    return ERRORS_DIR / f"{job_id}.json"


def _db_status(fname: str) -> Optional[str]:
    from app.database import engine
    from sqlalchemy import text
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT status FROM bhavcopy_files WHERE file_name=:fn"),
            {"fn": fname}
        ).first()
    return row[0] if row else None


def _match_inbox_file(filename: str):
    for pattern, source_id, fname_tpl in _INBOX_SOURCES:
        m = pattern.search(filename)
        if m:
            date_str = m.group(1)
            saved_fname = fname_tpl.format(date_str)
            return source_id, date_str, saved_fname
    return None


# -- Background download job --------------------------------------------------

def _run_download_job(job_id, source, dates, sleep_secs, force, error_file):
    download_fn = SOURCES[source]
    errors = []
    logger.info("[%s] Starting -- %d trading days, sleep=%ds, force=%s",
                job_id, len(dates), sleep_secs, force)
    for i, trade_date in enumerate(dates, 1):
        logger.info("[%s] [%d/%d] %s %s", job_id, i, len(dates), source, trade_date)
        try:
            success = download_fn(trade_date, force=force)
            if not success:
                raise RuntimeError("Download returned False")
        except Exception as exc:
            msg = str(exc)
            logger.error("[%s] Failed %s: %s", job_id, trade_date, msg)
            errors.append({"date": trade_date.isoformat(), "error": msg})
        if i < len(dates):
            logger.info("[%s] Sleeping %ds...", job_id, sleep_secs)
            time.sleep(sleep_secs)
    with open(error_file, "w") as f:
        json.dump(errors, f, indent=2)
    logger.info("[%s] Done -- %d ok, %d failed. Error file: %s",
                job_id, len(dates) - len(errors), len(errors), error_file)


# -- Endpoints ----------------------------------------------------------------

@router.get("/download", response_model=DownloadJobResponse)
def download_bhavcopy(
    source:     BhavSource    = Query(...,   description="Bhavcopy source"),
    start_date: str           = Query(...,   description="Start date YYYY-MM-DD"),
    end_date:   Optional[str] = Query(None,  description="End date YYYY-MM-DD (default: yesterday)"),
    sleep:      int           = Query(300,   description="Seconds between downloads (min 30)"),
    force:      bool          = Query(False, description="Re-download even if already downloaded"),
):
    """Trigger bulk historical bhavcopy download. Runs in background, returns immediately."""
    if sleep < MIN_SLEEP_SECONDS:
        raise HTTPException(status_code=400,
                            detail=f"sleep must be at least {MIN_SLEEP_SECONDS} seconds")
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400,
                            detail=f"Invalid start_date: {start_date}. Use YYYY-MM-DD.")
    try:
        end = (datetime.strptime(end_date, "%Y-%m-%d").date()
               if end_date else date.today() - timedelta(days=1))
    except ValueError:
        raise HTTPException(status_code=400,
                            detail=f"Invalid end_date: {end_date}. Use YYYY-MM-DD.")
    if start > end:
        raise HTTPException(status_code=400,
                            detail=f"start_date {start} is after end_date {end}")

    dates = _trading_days(start, end)
    if not dates:
        raise HTTPException(status_code=400, detail="No trading days found in the given range")

    source_str = source.value
    job_id     = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{source_str}"
    error_file = _error_file_path(job_id)

    threading.Thread(
        target=_run_download_job,
        args=(job_id, source_str, dates, sleep, force, error_file),
        daemon=True,
        name=f"bhavcopy-{job_id}",
    ).start()

    logger.info("[%s] Background thread started", job_id)

    return DownloadJobResponse(
        status        = "started",
        job_id        = job_id,
        source        = source_str,
        start_date    = start.isoformat(),
        end_date      = end.isoformat(),
        trading_days  = len(dates),
        sleep_seconds = sleep,
        force         = force,
        error_file    = str(error_file),
    )


@router.post("/sync-inbox", response_model=SyncInboxResponse)
def sync_inbox(force: bool = False):
    """
    Scan /app/inbox/ for original NSE/BSE bhavcopy files and register them.
    """
    if not INBOX_DIR.exists():
        raise HTTPException(status_code=500,
                            detail=f"Inbox directory not found: {INBOX_DIR}")

    synced:    list[str] = []
    can_force: list[str] = []
    blocked:   list[str] = []
    invalid:   list[str] = []

    all_files = sorted(
        f for f in INBOX_DIR.iterdir()
        if f.is_file() and f.suffix.lower() in (".csv", ".zip")
    )
    if not all_files:
        return SyncInboxResponse(synced=[], can_force=[], blocked=[], invalid=[])

    for src in all_files:
        info = _match_inbox_file(src.name)
        if not info:
            invalid.append(src.name)
            continue

        source_id, date_str, saved_fname = info
        trade_date = datetime.strptime(date_str, "%Y%m%d").date()
        dest       = date_dir(trade_date) / saved_fname
        status     = _db_status(saved_fname)

        if status == FileStatus.SYNCED:
            blocked.append(src.name)
            continue

        if dest.exists() and status == FileStatus.DOWNLOADED and not force:
            can_force.append(src.name)
            continue

        ok = register_file(src, force=force)
        if ok:
            synced.append(src.name)
        else:
            invalid.append(src.name)

    logger.info("[sync-inbox] Done -- synced=%d, can_force=%d, blocked=%d, invalid=%d",
                len(synced), len(can_force), len(blocked), len(invalid))

    return SyncInboxResponse(
        synced    = synced,
        can_force = can_force,
        blocked   = blocked,
        invalid   = invalid,
    )



def _run_parse_job(job_id: str, source: str, force: bool, result_file: Path):
    from app.cron.bhavcopy.sync import PARSERS
    parser = PARSERS.get(source)
    logger.info("[%s] Parse job started -- source=%s force=%s", job_id, source, force)
    try:
        result = parser.run(force=force)
    except Exception as exc:
        logger.error("[%s] Parse job failed: %s", job_id, exc, exc_info=True)
        result = {
            "source": source, "files_synced": 0, "files_failed": 0,
            "total_rows_synced": 0, "errors": [{"error": str(exc)}],
        }
    with open(result_file, "w") as f:
        json.dump(result, f, indent=2)
    logger.info("[%s] Parse job done -- synced=%s failed=%s rows=%s",
                job_id, result["files_synced"], result["files_failed"], result["total_rows_synced"])


@router.post("/parse", response_model=ParseJobResponse)
def parse_bhavcopy(
    source: BhavSource = Query(...,   description="Source to parse"),
    force:  bool       = Query(False, description="Re-parse already synced files"),
):
    """
    Parse all downloaded bhavcopy files for the given source.
    Runs in the background and returns immediately with a job_id.
    Check the result_file path for the final stats once the job completes.
    """
    from app.cron.bhavcopy.sync import PARSERS

    if source.value not in PARSERS:
        raise HTTPException(status_code=400, detail=f"No parser for source: {source.value}")

    job_id      = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_parse_{source.value}"
    result_file = _error_file_path(job_id)

    threading.Thread(
        target=_run_parse_job,
        args=(job_id, source.value, force, result_file),
        daemon=True,
        name=f"parse-{job_id}",
    ).start()

    logger.info("[%s] Parse background thread started", job_id)

    return ParseJobResponse(
        status      = "started",
        job_id      = job_id,
        source      = source.value,
        force       = force,
        result_file = str(result_file),
    )
