"""
Bhavcopy downloader — fetches EOD data files from NSE and BSE for a given date.

NSE Equity : nsearchives direct CSV  → BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv
NSE F&O    : nsearchives zip          → BhavCopy_NSE_FO_0_0_0_{YYYYMMDD}_F_0000.csv.zip
BSE Equity : BSE direct CSV URL
BSE F&O    : BSE direct CSV URL

Saved to <DATA_PATH>/bhavcopy/<YYYY-MM-DD>/<SOURCE>_<YYYYMMDD>.csv
DATA_PATH env var → Docker volume mount point.
"""

from __future__ import annotations

import io
import logging
import os
import zipfile
from datetime import date, datetime
from typing import Optional

import pandas as pd
import requests
from sqlalchemy import text

from app.database import engine
from app.cron.bhavcopy.common import gcs_blob_name, upload_df_to_gcs, GCS_BUCKET

logger = logging.getLogger(__name__)

_NSE_HEADERS = {
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

_BSE_HEADERS = {
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


def _file_name(source: str, trade_date: date) -> str:
    return f"{source}_{trade_date.strftime('%Y%m%d')}.csv"


def _record_status(file_name, trade_date, source, status, error=None):
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO bhavcopy_files (file_name,trade_date,source,status,error,updated_at)
            VALUES (:fn,:td,:src,:status,:error,datetime('now'))
            ON CONFLICT(file_name) DO UPDATE SET
                status=excluded.status, error=excluded.error, updated_at=datetime('now')
        """), {"fn": file_name, "td": trade_date.isoformat(), "src": source,
               "status": status, "error": error})


def _already_downloaded(file_name):
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT status FROM bhavcopy_files WHERE file_name=:fn"
        ), {"fn": file_name}).first()
    return row is not None and row[0] in ("downloaded", "synced")


def _nse_session():
    s = requests.Session()
    try:
        s.get("https://www.nseindia.com", headers=_NSE_HEADERS, timeout=15)
    except Exception:
        pass
    return s


def _read_csv_or_zip(content: bytes) -> pd.DataFrame:
    """Transparently handle plain CSV or zip-wrapped CSV response."""
    if content[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            csv_name = next((n for n in zf.namelist() if n.lower().endswith(".csv")), None)
            if not csv_name:
                raise ValueError(f"No CSV found in zip. Contents: {zf.namelist()}")
            return pd.read_csv(zf.open(csv_name))
    return pd.read_csv(io.BytesIO(content))


# ── NSE Equity ────────────────────────────────────────────────────────────────

def download_nse_eq(trade_date: date, force: bool = False) -> Optional[bool]:
    """
    Download NSE CM bhavcopy from nsearchives direct CSV.

    URL: https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv

    Columns include: ISIN, SctySrs (series), TckrSymb (symbol), OHLCV, etc.
    Filter SctySrs == 'EQ' for main-board equity.
    File may be served as plain CSV or zip — handled transparently.
    """
    fname = _file_name("NSE_EQ", trade_date)
    blob  = gcs_blob_name(trade_date, fname)

    if not force and _already_downloaded(fname):
        logger.info(f"[NSE_EQ] Already downloaded — skipping")
        return True

    date_str = trade_date.strftime("%Y%m%d")
    url = (f"https://nsearchives.nseindia.com/content/cm/"
           f"BhavCopy_NSE_CM_0_0_0_{date_str}_F_0000.csv")
    logger.info(f"[NSE_EQ] Downloading {url}")
    try:
        resp = _nse_session().get(url, headers=_NSE_HEADERS, timeout=60)
        if resp.status_code != 200:
            raise ValueError(f"HTTP {resp.status_code}")

        df = _read_csv_or_zip(resp.content)

        if df.empty:
            raise ValueError("Empty NSE EQ CSV")
        upload_df_to_gcs(df, blob)
        _record_status(fname, trade_date, "NSE_EQ", "downloaded")
        logger.info(f"[NSE_EQ] Uploaded {len(df)} rows -> gs://{GCS_BUCKET}/{blob}")
        return True
    except Exception as exc:
        logger.error(f"[NSE_EQ] Failed: {exc}", exc_info=True)
        _record_status(fname, trade_date, "NSE_EQ", "failed", str(exc))
        return None


# ── NSE F&O ───────────────────────────────────────────────────────────────────

def download_nse_fo(trade_date: date, force: bool = False) -> Optional[bool]:
    """Download NSE F&O bhavcopy zip from nsearchives and upload the CSV inside."""
    fname = _file_name("NSE_FO", trade_date)
    blob  = gcs_blob_name(trade_date, fname)

    if not force and _already_downloaded(fname):
        logger.info(f"[NSE_FO] Already downloaded — skipping")
        return True

    date_str = trade_date.strftime("%Y%m%d")
    url = (f"https://nsearchives.nseindia.com/content/fo/"
           f"BhavCopy_NSE_FO_0_0_0_{date_str}_F_0000.csv.zip")
    logger.info(f"[NSE_FO] Downloading {url}")
    try:
        resp = _nse_session().get(url, headers=_NSE_HEADERS, timeout=60)
        if resp.status_code != 200:
            raise ValueError(f"HTTP {resp.status_code}")

        df = _read_csv_or_zip(resp.content)

        if df.empty:
            raise ValueError("Empty F&O CSV")
        upload_df_to_gcs(df, blob)
        _record_status(fname, trade_date, "NSE_FO", "downloaded")
        logger.info(f"[NSE_FO] Uploaded {len(df)} rows -> gs://{GCS_BUCKET}/{blob}")
        return True
    except Exception as exc:
        logger.error(f"[NSE_FO] Failed: {exc}", exc_info=True)
        _record_status(fname, trade_date, "NSE_FO", "failed", str(exc))
        return None


# ── BSE Equity ────────────────────────────────────────────────────────────────

def download_bse_eq(trade_date: date, force: bool = False) -> Optional[bool]:
    """Download BSE equity bhavcopy (direct CSV/zip URL)."""
    fname    = _file_name("BSE_EQ", trade_date)
    blob     = gcs_blob_name(trade_date, fname)
    date_str = trade_date.strftime("%Y%m%d")

    if not force and _already_downloaded(fname):
        logger.info(f"[BSE_EQ] Already downloaded — skipping")
        return True

    url = (f"https://www.bseindia.com/download/BhavCopy/Equity/"
           f"BhavCopy_BSE_CM_0_0_0_{date_str}_F_0000.CSV")
    logger.info(f"[BSE_EQ] Downloading {url}")
    try:
        resp = requests.get(url, headers=_BSE_HEADERS, timeout=60)
        if resp.status_code != 200:
            raise ValueError(f"HTTP {resp.status_code}")

        df = _read_csv_or_zip(resp.content)

        if df.empty:
            raise ValueError("Empty BSE EQ response")
        upload_df_to_gcs(df, blob)
        _record_status(fname, trade_date, "BSE_EQ", "downloaded")
        logger.info(f"[BSE_EQ] Uploaded {len(df)} rows -> gs://{GCS_BUCKET}/{blob}")
        return True
    except Exception as exc:
        logger.error(f"[BSE_EQ] Failed: {exc}", exc_info=True)
        _record_status(fname, trade_date, "BSE_EQ", "failed", str(exc))
        return None


# ── BSE F&O ───────────────────────────────────────────────────────────────────

def download_bse_fo(trade_date: date, force: bool = False) -> Optional[bool]:
    """Download BSE F&O bhavcopy (direct CSV/zip URL)."""
    fname    = _file_name("BSE_FO", trade_date)
    blob     = gcs_blob_name(trade_date, fname)
    date_str = trade_date.strftime("%Y%m%d")

    if not force and _already_downloaded(fname):
        logger.info(f"[BSE_FO] Already downloaded — skipping")
        return True

    url = (f"https://www.bseindia.com/download/BhavCopy/Derivative/"
           f"BhavCopy_BSE_FO_0_0_0_{date_str}_F_0000.CSV")
    logger.info(f"[BSE_FO] Downloading {url}")
    try:
        resp = requests.get(url, headers=_BSE_HEADERS, timeout=60)
        if resp.status_code != 200:
            raise ValueError(f"HTTP {resp.status_code}")

        df = _read_csv_or_zip(resp.content)

        if df.empty:
            raise ValueError("Empty BSE FO response")
        upload_df_to_gcs(df, blob)
        _record_status(fname, trade_date, "BSE_FO", "downloaded")
        logger.info(f"[BSE_FO] Uploaded {len(df)} rows -> gs://{GCS_BUCKET}/{blob}")
        return True
    except Exception as exc:
        logger.error(f"[BSE_FO] Failed: {exc}", exc_info=True)
        _record_status(fname, trade_date, "BSE_FO", "failed", str(exc))
        return None


# ── Orchestrator ──────────────────────────────────────────────────────────────

def download_all(trade_date: Optional[date] = None, force: bool = False) -> dict:
    if trade_date is None:
        trade_date = date.today()
    logger.info(f"=== Bhavcopy download for {trade_date} force={force} ===")
    results = {
        "date":   trade_date.isoformat(),
        "nse_eq": bool(download_nse_eq(trade_date, force)),
        "nse_fo": bool(download_nse_fo(trade_date, force)),
        "bse_eq": bool(download_bse_eq(trade_date, force)),
        "bse_fo": bool(download_bse_fo(trade_date, force)),
    }
    results["success_count"] = sum(1 for k, v in results.items() if k != "date" and v)
    logger.info(f"=== Download complete: {results} ===")
    return results


if __name__ == "__main__":
    import sys
    import logging as _log
    _log.basicConfig(level=_log.INFO)
    force_flag = "--force" in sys.argv
    date_arg   = next((a for a in sys.argv[1:] if not a.startswith("--")), None)
    run_date   = datetime.strptime(date_arg, "%Y-%m-%d").date() if date_arg else date.today()
    download_all(run_date, force=force_flag)
