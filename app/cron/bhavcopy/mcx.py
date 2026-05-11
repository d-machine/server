"""
MCX bhavcopy — download via JSON API.

Saved as : <DATA_PATH>/bhavcopy/<YYYY-MM-DD>/BhavCopy_MCX_0_0_0_YYYYMMDD_F_0000.csv

Only download mode (no register — source is a JSON API, not a file).

CLI:
  python -m app.cron.bhavcopy.mcx --date 2026-01-01
  python -m app.cron.bhavcopy.mcx --date 2026-01-01 --force
  python -m app.cron.bhavcopy.mcx --date 2026-01-01 --instrument FUTCOM
"""
from __future__ import annotations

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

SOURCE     = "MCX"
_FNAME_TPL = "BhavCopy_MCX_0_0_0_{}_F_0000.csv"
_API_URL   = "https://www.mcxindia.com/backpage.aspx/GetDateWiseBhavCopy"

# instrument filter — 'ALL' retrieves every segment in one call
INSTRUMENTS = ["ALL", "FUTCOM", "FUTIDX", "OPTCOM", "OPTFUT"]

_MCX_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json; charset=UTF-8",
    "Origin": "https://www.mcxindia.com",
    "Referer": "https://www.mcxindia.com/market-data/bhav-copy",
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 "
        "Mobile Safari/537.36 Edg/147.0.0.0"
    ),
}


def _fname(trade_date) -> str:
    return _FNAME_TPL.format(trade_date.strftime("%Y%m%d"))


def _fetch_instrument(session: requests.Session, date_str: str, instrument: str) -> list[dict]:
    """POST to MCX JSON API, return list of row dicts for one instrument filter."""
    payload = {"Date": date_str, "InstrumentName": instrument}
    resp = session.post(_API_URL, json=payload, headers=_MCX_HEADERS, timeout=60)
    if resp.status_code != 200:
        raise ValueError(f"HTTP {resp.status_code} for instrument={instrument}")

    body = resp.json()
    # Response shape: {"d": {"Data": [...], ...}}
    d = body.get("d") or {}
    if isinstance(d, str):
        import json as _json
        d = _json.loads(d)
    data = d.get("Data") or []
    if not isinstance(data, list):
        raise ValueError(f"Unexpected 'Data' shape: {type(data)}")
    return data


def download(trade_date, force: bool = False, instrument: str = "ALL") -> bool:
    """
    Fetch bhavcopy from MCX JSON API and save as CSV.

    Args:
        trade_date: datetime.date
        force:      overwrite even if already downloaded
        instrument: one of INSTRUMENTS (default 'ALL')
    """
    if instrument not in INSTRUMENTS:
        raise ValueError(f"instrument must be one of {INSTRUMENTS}")

    fname = _fname(trade_date)
    blob  = gcs_blob_name(trade_date, fname)

    if not force and already_downloaded(fname):
        logger.info("[%s] %s already downloaded — skipping", SOURCE, trade_date)
        return True

    date_str = trade_date.strftime("%Y%m%d")
    logger.info("[%s] Fetching %s instrument=%s", SOURCE, date_str, instrument)

    session = requests.Session()
    # Warm up session cookie
    try:
        session.get("https://www.mcxindia.com", headers=_MCX_HEADERS, timeout=15)
    except Exception:
        pass

    try:
        rows = _fetch_instrument(session, date_str, instrument)
        if not rows:
            raise ValueError(f"No data returned for {date_str} instrument={instrument}")

        df = pd.DataFrame(rows)
        if df.empty:
            raise ValueError("Empty DataFrame after parsing response")

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
    parser = argparse.ArgumentParser(description="MCX bhavcopy downloader")
    parser.add_argument("--date", required=True, help="Trade date YYYY-MM-DD")
    parser.add_argument("--force", action="store_true", help="Overwrite if already downloaded")
    parser.add_argument(
        "--instrument", default="ALL", choices=INSTRUMENTS,
        help="Instrument filter (default: ALL)"
    )
    args = parser.parse_args()

    d = datetime.strptime(args.date, "%Y-%m-%d").date()
    ok = download(d, force=args.force, instrument=args.instrument)
    sys.exit(0 if ok else 1)
