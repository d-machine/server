"""
MCX bhavcopy — download via ASP.NET form submission (CSV export button).

The JSON API used by mcx.py is blocked for non-browser IPs. This module
instead mimics the browser flow:
  1. GET the bhavcopy page to capture __VIEWSTATE and session cookies
  2. POST back with the date and __EVENTTARGET set to the CSV export link
  3. Parse the CSV response and upload to GCS

Saved as : bhavcopy/<YYYY-MM-DD>/BhavCopy_MCX_0_0_0_YYYYMMDD_F_0000.csv

CLI:
  python -m app.cron.bhavcopy.mcx2 --date 2025-01-01
  python -m app.cron.bhavcopy.mcx2 --date 2025-01-01 --force
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
    GCS_BUCKET, gcs_blob_name, upload_df_to_gcs, record_status, already_downloaded,
)

logger = logging.getLogger(__name__)

SOURCE     = "MCX"
_FNAME_TPL = "BhavCopy_MCX_0_0_0_{}_F_0000.csv"
_PAGE_URL  = "https://www.mcxindia.com/market-data/bhavcopy"

_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
              "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "en-US,en;q=0.9,en-IN;q=0.8",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "Origin": "https://www.mcxindia.com",
    "Pragma": "no-cache",
    "Referer": "https://www.mcxindia.com/market-data/bhavcopy",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 "
                  "Mobile Safari/537.36 Edg/147.0.0.0",
    "sec-ch-ua": '"Microsoft Edge";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
    "sec-ch-ua-mobile": "?1",
    "sec-ch-ua-platform": '"Android"',
}


def _fname(trade_date) -> str:
    return _FNAME_TPL.format(trade_date.strftime("%Y%m%d"))


# Maps MCX website CSV export column names → what the parser expects
_COL_MAP = {
    "Date":               "DateDisplay",
    "Instrument Name":    "InstrumentName",
    "Expiry Date":        "ExpiryDate",
    "Option Type":        "OptionType",
    "Strike Price":       "StrikePrice",
    "Previous Close":     "PreviousClose",
    "Volume(Lots)":       "Volume",
    "Volume(In 000's)":   "VolumeInThousands",
    "Value(Lacs)":        "Value",
    "Open Interest(Lots)":"OpenInterest",
    # Symbol, Open, High, Low, Close already match
}


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Rename website CSV columns to match JSON API column names the parser expects."""
    df.columns = df.columns.str.strip()
    df = df.rename(columns=_COL_MAP)
    logger.debug("[%s] Columns after normalize: %s", SOURCE, df.columns.tolist())
    return df


def _extract_hidden_fields(html: str) -> dict:
    """Extract all <input type="hidden"> fields from the page."""
    fields = {}
    for m in re.finditer(
        r'<input[^>]+type=["\']hidden["\'][^>]*>', html, re.IGNORECASE
    ):
        tag = m.group(0)
        name  = re.search(r'name=["\']([^"\']+)["\']', tag)
        value = re.search(r'value=["\']([^"\']*)["\']', tag)
        if name:
            fields[name.group(1)] = value.group(1) if value else ""
    return fields


def download(trade_date, force: bool = False) -> bool:
    fname = _fname(trade_date)
    blob  = gcs_blob_name(trade_date, fname)

    if not force and already_downloaded(fname):
        logger.info("[%s] %s already downloaded — skipping", SOURCE, trade_date)
        return None

    date_str = trade_date.strftime("%Y%m%d")
    logger.info("[%s] Fetching page for VIEWSTATE — date %s", SOURCE, date_str)

    session = requests.Session()
    try:
        # Step 1 — GET page to capture VIEWSTATE and session cookies
        get_resp = session.get(_PAGE_URL, headers=_HEADERS, timeout=30)
        if get_resp.status_code != 200:
            raise ValueError(f"GET page returned HTTP {get_resp.status_code}")

        hidden = _extract_hidden_fields(get_resp.text)
        if not hidden.get("__VIEWSTATE"):
            raise ValueError("Could not extract __VIEWSTATE from page")

        # Step 2 — Build form and POST to trigger CSV export
        form = {
            **hidden,
            "__EVENTTARGET":  "ctl00$cph_InnerContainerRight$C001$lnkExpToCSV",
            "__EVENTARGUMENT": "",
            "ctl00$cph_nav_container_topbar_language$T9DC6B4FB015$ctl00$ctl00$langsSelect": _PAGE_URL,
            "ctl00$cph_nav_container_searchbox$T9DC6B4FB016$radDDL": "Get Quote",
            "ctl00_cph_nav_container_searchbox_T9DC6B4FB016_radDDL_ClientState": "",
            "ctl00$cph_nav_container_searchbox_mobile$T9DC6B4FB019$ddlProducts": "GetQuote",
            "ctl00$cph_nav_container_navbar_header_mobile_main_menu2$T9DC6B4FB018$ctl00$ctl00$langsSelect": _PAGE_URL,
            "ctl00_cph_InnerContainerRight_BreadCrumb_T9DC6B4FB006_ctl00_ctl00_Breadcrumb_ClientState": "",
            "ctl00$cph_InnerContainerRight$C001$hdnInstrumentName": "ALL",
            "ctl00$cph_InnerContainerRight$C001$txtDate_hid_val": date_str,
            "ctl00$cph_InnerContainerRight$C001$hdnCommodityInstrumentName": "",
            "ctl00$cph_InnerContainerRight$C001$ddlSymbols": "ALL",
            "ddlSymbols_ClientState": "",
            "ctl00$cph_InnerContainerRight$C001$hdnSymbols": "",
            "ctl00$cph_InnerContainerRight$C001$hdnExpiry": "",
            "ctl00$cph_InnerContainerRight$C001$hdnFromDate": "",
            "ctl00$cph_InnerContainerRight$C001$hdnToDate": "",
            "ctl00_cph_InnerContainerRight_C001_rgBhavCopy_ClientState": "",
            "ctl00$hdnCurrentCulture": "en",
        }

        post_headers = {**_HEADERS, "Content-Type": "application/x-www-form-urlencoded"}
        post_resp = session.post(_PAGE_URL, data=form, headers=post_headers, timeout=60)
        if post_resp.status_code != 200:
            raise ValueError(f"POST returned HTTP {post_resp.status_code}")

        content_type = post_resp.headers.get("Content-Type", "")
        if "text/html" in content_type:
            raise ValueError(
                "Got HTML response instead of CSV — Akamai may be blocking, "
                "or no data exists for this date"
            )

        # Step 3 — Parse and normalize CSV response
        df = pd.read_csv(io.BytesIO(post_resp.content))
        if df.empty:
            raise ValueError("Empty CSV response")

        df = _normalize(df)
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
    parser = argparse.ArgumentParser(description="MCX bhavcopy downloader (form-based)")
    parser.add_argument("--date", required=True, help="Trade date YYYY-MM-DD")
    parser.add_argument("--force", action="store_true", help="Overwrite if already downloaded")
    args = parser.parse_args()

    d = datetime.strptime(args.date, "%Y-%m-%d").date()
    ok = download(d, force=args.force)
    sys.exit(0 if ok else 1)
