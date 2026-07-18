"""
MCX bhavcopy — download via the page's internal JSON endpoint.

MCX redesigned /market-data/bhavcopy as a client-rendered page (no more
__VIEWSTATE / ASP.NET postback). The page's own JS (assets/customjs/BhavCopy.js)
fetches data from a same-origin endpoint and exports it client-side, so this
module mimics that call instead:
  1. GET the bhavcopy page to establish session cookies
  2. GET /market-data/bhavcopy/GetDateWiseBhavCopy with InstrumentName + fromDate
  3. Parse the JSON response and upload to GCS

Saved as : bhavcopy/<YYYY-MM-DD>/BhavCopy_MCX_0_0_0_YYYYMMDD_F_0000.csv

CLI:
  python -m app.cron.bhavcopy.mcx2 --date 2025-01-01
  python -m app.cron.bhavcopy.mcx2 --date 2025-01-01 --force
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime

import pandas as pd
import requests

from app.cron.bhavcopy.constants import FileStatus
from app.cron.bhavcopy.common import (
    GCS_BUCKET, gcs_blob_name, upload_df_to_gcs, record_status, already_downloaded,
)

logger = logging.getLogger(__name__)

SOURCE     = "MCX"
_FNAME_TPL = "BhavCopy_MCX_0_0_0_{}_F_0000.csv"
_PAGE_URL  = "https://www.mcxindia.com/market-data/bhavcopy"
_API_URL   = _PAGE_URL + "/GetDateWiseBhavCopy"

_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9,en-IN;q=0.8",
    "Referer": _PAGE_URL,
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 "
                  "Mobile Safari/537.36 Edg/147.0.0.0",
}


def _fname(trade_date) -> str:
    return _FNAME_TPL.format(trade_date.strftime("%Y%m%d"))


def download(trade_date, force: bool = False) -> bool:
    fname = _fname(trade_date)
    blob  = gcs_blob_name(trade_date, fname)

    if not force and already_downloaded(fname):
        logger.info("[%s] %s already downloaded — skipping", SOURCE, trade_date)
        return None

    date_str = trade_date.strftime("%d/%m/%Y")
    logger.info("[%s] Fetching bhavcopy — date %s", SOURCE, date_str)

    session = requests.Session()
    try:
        # Step 1 — GET page to establish session cookies
        get_resp = session.get(_PAGE_URL, headers=_HEADERS, timeout=30)
        if get_resp.status_code != 200:
            raise ValueError(f"GET page returned HTTP {get_resp.status_code}")

        # Step 2 — GET the JSON endpoint the page's own JS calls
        api_resp = session.get(
            _API_URL,
            params={"InstrumentName": "ALL", "fromDate": date_str},
            headers=_HEADERS,
            timeout=60,
        )
        if api_resp.status_code != 200:
            raise ValueError(f"API returned HTTP {api_resp.status_code}")

        body = api_resp.json()
        if not body.get("IsSuccess"):
            raise ValueError(f"API reported failure: {body.get('Message')}")

        rows = body.get("Data") or []
        if not rows:
            raise ValueError("Empty Data in API response")

        # Step 3 — Parse response
        df = pd.DataFrame(rows)
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
    args = parser.parse_args()

    d = datetime.strptime(args.date, "%Y-%m-%d").date()
    ok = download(d, force=args.force)
    sys.exit(0 if ok else 1)
