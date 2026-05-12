"""
Shared utilities for all bhavcopy scripts.
"""
from __future__ import annotations

import io
import logging
import os
from datetime import date
from typing import Optional

import pandas as pd
import requests
from google.cloud import storage as _gcs
from sqlalchemy import text

from app.database import engine
from app.cron.bhavcopy.constants import FileStatus

logger = logging.getLogger(__name__)

GCS_BUCKET = os.getenv("GCS_BHAVCOPY_BUCKET", "arthdesk-bhavcopy")

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

_gcs_client: _gcs.Client | None = None


def _bucket() -> _gcs.Bucket:
    global _gcs_client
    if _gcs_client is None:
        _gcs_client = _gcs.Client()
    return _gcs_client.bucket(GCS_BUCKET)


def gcs_blob_name(trade_date: date, fname: str) -> str:
    return f"bhavcopy/{trade_date.isoformat()}/{fname}"


def gcs_blob_exists(blob_name: str) -> bool:
    return _bucket().blob(blob_name).exists()


def upload_df_to_gcs(df: pd.DataFrame, blob_name: str) -> None:
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    _bucket().blob(blob_name).upload_from_string(buf.getvalue(), content_type="text/csv")


def download_df_from_gcs(blob_name: str, **read_csv_kwargs) -> pd.DataFrame:
    data = _bucket().blob(blob_name).download_as_bytes()
    return pd.read_csv(io.BytesIO(data), **read_csv_kwargs)


def download_bytes_from_gcs(blob_name: str) -> bytes:
    return _bucket().blob(blob_name).download_as_bytes()


def download_df_chunks_from_gcs(blob_name: str, chunksize: int = 5_000):
    """Download blob once, return a chunked CSV reader to avoid large DataFrames."""
    data = _bucket().blob(blob_name).download_as_bytes()
    return pd.read_csv(io.BytesIO(data), dtype=str, chunksize=chunksize)


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
