"""
BSE Equity bhavcopy — download or register.

Saved as : <DATA_PATH>/bhavcopy/<YYYY-MM-DD>/BhavCopy_BSE_CM_0_0_0_YYYYMMDD_F_0000.CSV

Two modes:
  download(trade_date)  — fetches CSV directly from BSE, saves
  register(file_path)   — takes a .CSV from inbox, copies to data dir

CLI:
  python -m app.cron.bhavcopy.bse_eq --date 2026-01-01
  python -m app.cron.bhavcopy.bse_eq --file /app/inbox/BhavCopy_BSE_CM_0_0_0_20260101_F_0000.CSV
"""
from __future__ import annotations

import io
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

from app.cron.bhavcopy.constants import FileStatus
from app.cron.bhavcopy.common import (
    BSE_HEADERS, GCS_BUCKET, gcs_blob_name, gcs_blob_exists, upload_df_to_gcs,
    record_status, already_downloaded,
)

logger = logging.getLogger(__name__)

SOURCE     = "BSE_EQ"
_FNAME_TPL = "BhavCopy_BSE_CM_0_0_0_{}_F_0000.CSV"
_URL_TPL   = ("https://www.bseindia.com/download/BhavCopy/Equity/"
              "BhavCopy_BSE_CM_0_0_0_{}_F_0000.CSV")
_INBOX_RE  = re.compile(
    r"BhavCopy_BSE_CM_0_0_0_(\d{8})_F_0000\.csv$", re.IGNORECASE
)


def _fname(trade_date) -> str:
    return _FNAME_TPL.format(trade_date.strftime("%Y%m%d"))


def download(trade_date, force: bool = False) -> bool:
    fname = _fname(trade_date)
    blob  = gcs_blob_name(trade_date, fname)

    if not force and already_downloaded(fname):
        logger.info("[%s] %s already downloaded — skipping", SOURCE, trade_date)
        return True

    url = _URL_TPL.format(trade_date.strftime("%Y%m%d"))
    logger.info("[%s] Downloading %s", SOURCE, url)
    try:
        resp = requests.get(url, headers=BSE_HEADERS, timeout=60)
        if resp.status_code != 200:
            raise ValueError(f"HTTP {resp.status_code}")
        df = pd.read_csv(io.StringIO(resp.text))
        if df.empty:
            raise ValueError("Empty response")
        upload_df_to_gcs(df, blob)
        record_status(fname, trade_date, SOURCE, FileStatus.DOWNLOADED)
        logger.info("[%s] Uploaded %d rows -> gs://%s/%s", SOURCE, len(df), GCS_BUCKET, blob)
        return True
    except Exception as exc:
        logger.error("[%s] Failed %s: %s", SOURCE, trade_date, exc, exc_info=True)
        record_status(fname, trade_date, SOURCE, FileStatus.DOWNLOAD_FAILED, str(exc))
        return False


def register(file_path: Path, force: bool = False) -> bool:
    match = _INBOX_RE.search(file_path.name)
    if not match:
        logger.error("[%s] Filename does not match BSE CM pattern: %s", SOURCE, file_path.name)
        return False

    trade_date = datetime.strptime(match.group(1), "%Y%m%d").date()
    fname = _fname(trade_date)
    blob  = gcs_blob_name(trade_date, fname)

    if not file_path.exists():
        logger.error("[%s] File not found: %s", SOURCE, file_path)
        return False

    db_status = _get_db_status(fname)
    if db_status == FileStatus.SYNCED:
        logger.error("[%s] Already synced — cannot override: %s", SOURCE, fname)
        return False

    if gcs_blob_exists(blob) and not force:
        logger.error("[%s] Blob exists in GCS, use --force to overwrite: gs://%s/%s",
                     SOURCE, GCS_BUCKET, blob)
        return False

    try:
        df = pd.read_csv(file_path)
        if df.empty:
            raise ValueError("Empty file")
        upload_df_to_gcs(df, blob)
        record_status(fname, trade_date, SOURCE, FileStatus.DOWNLOADED)
        logger.info("[%s] Registered %s -> gs://%s/%s (%d rows)",
                    SOURCE, file_path.name, GCS_BUCKET, blob, len(df))
        return True
    except Exception as exc:
        logger.error("[%s] Failed to register %s: %s", SOURCE, file_path.name, exc, exc_info=True)
        return False


def _get_db_status(fname: str):
    from app.database import engine
    from sqlalchemy import text
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT status FROM bhavcopy_files WHERE file_name=:fn"), {"fn": fname}
        ).first()
    return row[0] if row else None


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="BSE Equity bhavcopy downloader/register")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--date", help="Trade date YYYY-MM-DD (URL mode)")
    group.add_argument("--file", help="Path to inbox file (local mode)")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if args.date:
        d = datetime.strptime(args.date, "%Y-%m-%d").date()
        ok = download(d, force=args.force)
    else:
        ok = register(Path(args.file).resolve(), force=args.force)

    sys.exit(0 if ok else 1)
