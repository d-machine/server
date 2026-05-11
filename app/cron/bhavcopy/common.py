"""
Shared utilities for all bhavcopy scripts.
"""
from __future__ import annotations

import logging
import os
from datetime import date
from pathlib import Path
from typing import Optional

import requests
from sqlalchemy import text

from app.database import engine
from app.cron.bhavcopy.constants import FileStatus

logger = logging.getLogger(__name__)

DATA_DIR     = Path(os.getenv("DATA_PATH", "data"))
BHAVCOPY_DIR = DATA_DIR / "bhavcopy"

NSE_HEADERS = {
    "sec-ch-ua-platform": '"Android"',
    "Referer": "https://www.nseindia.com/all-reports/",
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 "
                  "Mobile Safari/537.36 Edg/147.0.0.0",
    "Accept": "*/*",
    "sec-ch-ua": '"Microsoft Edge";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
    "sec-ch-ua-mobile": "?1",
}

BSE_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
              "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "accept-language": "en-US,en;q=0.9,en-IN;q=0.8",
    "referer": "https://www.bseindia.com/markets/marketinfo/bhavcopy",
    "sec-ch-ua": '"Microsoft Edge";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
    "sec-ch-ua-mobile": "?1",
    "sec-ch-ua-platform": '"Android"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "same-origin",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
    "user-agent": "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 "
                  "Mobile Safari/537.36 Edg/147.0.0.0",
}


def date_dir(trade_date: date) -> Path:
    d = BHAVCOPY_DIR / trade_date.isoformat()
    d.mkdir(parents=True, exist_ok=True)
    return d


def record_status(fname: str, trade_date: date, source: str,
                  status: FileStatus, error: Optional[str] = None):
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO bhavcopy_files (file_name, trade_date, source, status, error, updated_at)
            VALUES (:fn, :td, :src, :status, :error, datetime('now'))
            ON CONFLICT(file_name) DO UPDATE SET
                status=excluded.status, error=excluded.error, updated_at=datetime('now')
        """), {"fn": fname, "td": trade_date.isoformat(), "src": source,
               "status": int(status), "error": error})


def already_downloaded(fname: str) -> bool:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT status FROM bhavcopy_files WHERE file_name=:fn"),
            {"fn": fname}
        ).first()
    return row is not None and row[0] in (FileStatus.DOWNLOADED, FileStatus.SYNCED)


def nse_session() -> requests.Session:
    s = requests.Session()
    try:
        s.get("https://www.nseindia.com", headers=NSE_HEADERS, timeout=15)
    except Exception:
        pass
    return s
