"""
AMFI NAV bhavcopy — download for a single date.

Hits the AMFI portal, parses NAV data by matching semicolon count from header,
and saves a clean CSV.

Saved as : <DATA_PATH>/bhavcopy/<YYYY-MM-DD>/BhavCopy_AMFI_NAV_0_0_0_YYYYMMDD_F_0000.csv

CLI:
  python -m app.cron.bhavcopy.amfi --date 2026-01-01
  python -m app.cron.bhavcopy.amfi --date 2026-01-01 --force
"""
from __future__ import annotations

import io
import logging
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

from app.cron.bhavcopy.constants import FileStatus
from app.cron.bhavcopy.common import (
    GCS_BUCKET, gcs_blob_name, upload_df_to_gcs, record_status, already_downloaded,
)

logger = logging.getLogger(__name__)

SOURCE     = "AMFI_NAV"
_FNAME_TPL = "BhavCopy_AMFI_NAV_0_0_0_{}_F_0000.csv"
_URL_TPL   = "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx?frmdt={}"


def _fname(trade_date) -> str:
    return _FNAME_TPL.format(trade_date.strftime("%Y%m%d"))


def _parse(text: str) -> pd.DataFrame:
    """
    Keep only rows whose semicolon count matches the header row.
    This filters out category headers, fund house names, and blank lines.
    """
    lines = text.strip().splitlines()
    if not lines:
        return pd.DataFrame()

    header = lines[0]
    n = header.count(";")

    data_lines = [header] + [l for l in lines[1:] if l.count(";") == n]
    if len(data_lines) <= 1:
        return pd.DataFrame()

    return pd.read_csv(io.StringIO("\n".join(data_lines)), sep=";", dtype=str)


def download(trade_date, force: bool = False) -> bool:
    fname = _fname(trade_date)
    blob  = gcs_blob_name(trade_date, fname)

    if not force and already_downloaded(fname):
        logger.info("[%s] %s already downloaded — skipping", SOURCE, trade_date)
        return None

    date_str = trade_date.strftime("%d-%b-%Y")   # e.g. 01-Jan-2026
    url = _URL_TPL.format(date_str)
    logger.info("[%s] Downloading %s", SOURCE, url)

    try:
        resp = requests.get(url, timeout=60)
        if resp.status_code != 200:
            raise ValueError(f"HTTP {resp.status_code}")

        df = _parse(resp.text)
        if df.empty:
            raise ValueError("No data rows found after parsing")

        upload_df_to_gcs(df, blob)
        record_status(fname, trade_date, SOURCE, FileStatus.DOWNLOADED)
        logger.info("[%s] Uploaded %d rows -> gs://%s/%s", SOURCE, len(df), GCS_BUCKET, blob)
        return True

    except Exception as exc:
        logger.error("[%s] Failed %s: %s", SOURCE, trade_date, exc, exc_info=True)
        record_status(fname, trade_date, SOURCE, FileStatus.DOWNLOAD_FAILED, str(exc))
        return False


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="AMFI NAV downloader")
    parser.add_argument("--date", required=True, help="Trade date YYYY-MM-DD")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    d  = datetime.strptime(args.date, "%Y-%m-%d").date()
    ok = download(d, force=args.force)
    sys.exit(0 if ok else 1)
