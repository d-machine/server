"""
Bhavcopy sync — parses downloaded CSV files and upserts prices into the DB.

Queries bhavcopy_files WHERE status='downloaded', processes each file
by source type, upserts into daily_prices + latest_prices, then marks
status='synced'. Failed files get status='failed' with the error message.

Per-source parsers return a list of price dicts:
  {instrument_id, trade_date, open, high, low, close, volume, source}

Only instruments already in the DB (matched by ISIN or NSE symbol) get prices.
Unknown instruments are counted and logged but not created here.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Optional

import pandas as pd
from sqlalchemy import text
from app.cron.bhavcopy.constants import FileStatus
from app.cron.bhavcopy.common import gcs_blob_name, gcs_blob_exists, download_df_from_gcs

from app.database import engine
from app.cron.fetch_prices import _upsert_daily as _upsert_daily_prices, _upsert_latest as _upsert_latest_prices

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _rupees_to_paise(val) -> int:
    try:
        return max(0, int(round(float(val) * 100)))
    except (TypeError, ValueError):
        return 0


def _isin_map(conn) -> dict[str, int]:
    rows = conn.execute(text(
        "SELECT isin, instrument_id FROM instruments WHERE isin IS NOT NULL"
    )).fetchall()
    return {r[0]: r[1] for r in rows}


def _nse_symbol_map(conn) -> dict[str, int]:
    rows = conn.execute(text("""
        SELECT ie.nse_symbol, ie.instrument_id
        FROM instrument_equity ie
        WHERE ie.nse_symbol IS NOT NULL
    """)).fetchall()
    return {r[0]: r[1] for r in rows}


def _mark_status(file_name: str, status, rows_synced: Optional[int] = None,
                 error: Optional[str] = None) -> None:
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE bhavcopy_files
            SET status = :status, rows_synced = :rows, error = :error,
                updated_at = datetime('now')
            WHERE file_name = :fn
        """), {"status": int(status) if hasattr(status, "value") else status, "rows": rows_synced, "error": error, "fn": file_name})


# ── Per-source parsers ────────────────────────────────────────────────────────

def _parse_nse_eq(df: pd.DataFrame, conn) -> list[dict]:
    """Parse NSE equity bhavcopy CSV into price dicts."""
    df.columns = [c.strip() for c in df.columns]

    # EQ series only
    if "SctySrs" in df.columns:
        df = df[df["SctySrs"].str.strip() == "EQ"].copy()
    elif "Series" in df.columns:
        df = df[df["Series"].str.strip() == "EQ"].copy()

    isin_col   = next((c for c in df.columns if "isin" in c.lower()), None)
    close_col  = next((c for c in df.columns if c.lower() in ("clspric", "close", "last_price", "prevclose")), None)
    open_col   = next((c for c in df.columns if c.lower() in ("opnpric", "open", "open_price")), None)
    high_col   = next((c for c in df.columns if c.lower() in ("hghpric", "high", "day_high")), None)
    low_col    = next((c for c in df.columns if c.lower() in ("lwpric", "low", "day_low")), None)
    vol_col    = next((c for c in df.columns if c.lower() in ("ttltradgvol", "tottrdqty", "total_traded_quantity")), None)
    date_col   = next((c for c in df.columns if c.lower() in ("timestamp", "date1", "trade_date")), None)

    if not isin_col or not close_col:
        logger.warning(f"[NSE_EQ] Missing required columns. Found: {df.columns.tolist()}")
        return []

    isin_to_id = _isin_map(conn)
    rows = []
    for _, r in df.iterrows():
        isin = str(r.get(isin_col, "")).strip()
        iid  = isin_to_id.get(isin)
        if not iid:
            continue

        close = _rupees_to_paise(r.get(close_col))
        if close <= 0:
            continue

        trade_date = None
        if date_col and r.get(date_col):
            try:
                trade_date = pd.to_datetime(str(r[date_col])).strftime("%Y-%m-%d")
            except Exception:
                pass

        rows.append({
            "instrument_id": iid,
            "trade_date":    trade_date or date.today().isoformat(),
            "open":          _rupees_to_paise(r.get(open_col)) if open_col else None,
            "high":          _rupees_to_paise(r.get(high_col)) if high_col else None,
            "low":           _rupees_to_paise(r.get(low_col))  if low_col  else None,
            "close":         close,
            "volume":        int(r.get(vol_col, 0) or 0) if vol_col else 0,
            "source":        "NSE",
        })
    return rows


def _parse_nse_fo(df: pd.DataFrame, conn) -> list[dict]:
    """Parse NSE F&O bhavcopy CSV into price dicts."""
    df.columns = [c.strip() for c in df.columns]

    isin_col   = next((c for c in df.columns if "isin" in c.lower()), None)
    settle_col = next((c for c in df.columns if c.lower() in ("sttlmntpric", "settle_pr", "settlprice")), None)
    close_col  = next((c for c in df.columns if c.lower() in ("clspric", "close")), None)
    open_col   = next((c for c in df.columns if c.lower() in ("opnpric", "open")), None)
    high_col   = next((c for c in df.columns if c.lower() in ("hghpric", "high")), None)
    low_col    = next((c for c in df.columns if c.lower() in ("lwpric", "low")), None)
    vol_col    = next((c for c in df.columns if c.lower() in ("ttltradgvol", "contracts")), None)
    date_col   = next((c for c in df.columns if c.lower() in ("timestamp", "expdt", "xprydt")), None)

    price_col = settle_col or close_col
    if not isin_col or not price_col:
        logger.warning(f"[NSE_FO] Missing required columns. Found: {df.columns.tolist()}")
        return []

    isin_to_id = _isin_map(conn)
    rows = []
    for _, r in df.iterrows():
        isin = str(r.get(isin_col, "")).strip()
        if not isin or len(isin) != 12:
            continue
        iid = isin_to_id.get(isin)
        if not iid:
            continue

        close = _rupees_to_paise(r.get(price_col))
        if close <= 0:
            continue

        rows.append({
            "instrument_id": iid,
            "trade_date":    date.today().isoformat(),
            "open":          _rupees_to_paise(r.get(open_col)) if open_col else None,
            "high":          _rupees_to_paise(r.get(high_col)) if high_col else None,
            "low":           _rupees_to_paise(r.get(low_col))  if low_col  else None,
            "close":         close,
            "volume":        int(r.get(vol_col, 0) or 0) if vol_col else 0,
            "source":        "NSE",
        })
    return rows


def _parse_bse_eq(df: pd.DataFrame, conn) -> list[dict]:
    """Parse BSE equity bhavcopy CSV into price dicts."""
    df.columns = [c.strip() for c in df.columns]

    isin_col   = next((c for c in df.columns if "isin" in c.lower()), None)
    close_col  = next((c for c in df.columns if c.lower() in ("clspric", "close", "close_price")), None)
    open_col   = next((c for c in df.columns if c.lower() in ("opnpric", "open", "open_price")), None)
    high_col   = next((c for c in df.columns if c.lower() in ("hghpric", "high", "high_price")), None)
    low_col    = next((c for c in df.columns if c.lower() in ("lwpric", "low", "low_price")), None)
    vol_col    = next((c for c in df.columns if c.lower() in ("ttltradgvol", "no_of_shares", "volume")), None)

    if not isin_col or not close_col:
        logger.warning(f"[BSE_EQ] Missing required columns. Found: {df.columns.tolist()}")
        return []

    # Only equity series
    series_col = next((c for c in df.columns if c.lower() in ("sctyrs", "series", "sctysrs")), None)
    if series_col:
        equity_series = {"A", "B", "T", "XT", "X", "Z", "M", "MT"}
        df = df[df[series_col].str.strip().isin(equity_series)].copy()

    isin_to_id = _isin_map(conn)

    # Skip ISINs already priced by NSE today
    nse_isins: set[str] = set()
    today = date.today().isoformat()
    rows_nse = conn.execute(text("""
        SELECT i.isin FROM daily_prices dp
        JOIN instruments i ON dp.instrument_id = i.instrument_id
        WHERE dp.trade_date = :d AND dp.source = 'NSE'
    """), {"d": today}).fetchall()
    nse_isins = {r[0] for r in rows_nse}

    rows = []
    for _, r in df.iterrows():
        isin = str(r.get(isin_col, "")).strip()
        if isin in nse_isins:
            continue
        iid = isin_to_id.get(isin)
        if not iid:
            continue

        close = _rupees_to_paise(r.get(close_col))
        if close <= 0:
            continue

        rows.append({
            "instrument_id": iid,
            "trade_date":    today,
            "open":          _rupees_to_paise(r.get(open_col)) if open_col else None,
            "high":          _rupees_to_paise(r.get(high_col)) if high_col else None,
            "low":           _rupees_to_paise(r.get(low_col))  if low_col  else None,
            "close":         close,
            "volume":        int(r.get(vol_col, 0) or 0) if vol_col else 0,
            "source":        "BSE",
        })
    return rows


def _parse_bse_fo(df: pd.DataFrame, conn) -> list[dict]:
    """Parse BSE F&O bhavcopy CSV into price dicts."""
    df.columns = [c.strip() for c in df.columns]

    isin_col   = next((c for c in df.columns if "isin" in c.lower()), None)
    close_col  = next((c for c in df.columns if c.lower() in ("clspric", "close", "settlementprice", "settleprice")), None)
    open_col   = next((c for c in df.columns if c.lower() in ("opnpric", "open")), None)
    high_col   = next((c for c in df.columns if c.lower() in ("hghpric", "high")), None)
    low_col    = next((c for c in df.columns if c.lower() in ("lwpric", "low")), None)
    vol_col    = next((c for c in df.columns if c.lower() in ("ttltradgvol", "noof_contracts", "volume")), None)

    if not isin_col or not close_col:
        logger.warning(f"[BSE_FO] Missing required columns. Found: {df.columns.tolist()}")
        return []

    isin_to_id = _isin_map(conn)
    today = date.today().isoformat()
    rows = []
    for _, r in df.iterrows():
        isin = str(r.get(isin_col, "")).strip()
        if not isin or len(isin) != 12:
            continue
        iid = isin_to_id.get(isin)
        if not iid:
            continue

        close = _rupees_to_paise(r.get(close_col))
        if close <= 0:
            continue

        rows.append({
            "instrument_id": iid,
            "trade_date":    today,
            "open":          _rupees_to_paise(r.get(open_col)) if open_col else None,
            "high":          _rupees_to_paise(r.get(high_col)) if high_col else None,
            "low":           _rupees_to_paise(r.get(low_col))  if low_col  else None,
            "close":         close,
            "volume":        int(r.get(vol_col, 0) or 0) if vol_col else 0,
            "source":        "BSE",
        })
    return rows


_PARSERS = {
    "NSE_EQ": _parse_nse_eq,
    "NSE_FO": _parse_nse_fo,
    "BSE_EQ": _parse_bse_eq,
    "BSE_FO": _parse_bse_fo,
}


# ── Main sync ─────────────────────────────────────────────────────────────────

def sync_file(row: dict) -> dict:
    """Sync a single bhavcopy_files row. Returns {file_name, rows_synced, status}."""
    file_name  = row["file_name"]
    source     = row["source"]
    trade_date = row["trade_date"]

    blob_name = gcs_blob_name(date.fromisoformat(trade_date), file_name)
    if not gcs_blob_exists(blob_name):
        err = f"Blob not found in GCS: {blob_name}"
        logger.error(f"[SYNC] {err}")
        _mark_status(file_name, FileStatus.SYNC_FAILED, error=err)
        return {"file_name": file_name, "status": "sync_failed", "rows_synced": 0}

    parser = _PARSERS.get(source)
    if not parser:
        err = f"No parser for source '{source}'"
        logger.error(f"[SYNC] {err}")
        _mark_status(file_name, FileStatus.SYNC_FAILED, error=err)
        return {"file_name": file_name, "status": "sync_failed", "rows_synced": 0}

    logger.info(f"[SYNC] Processing {file_name} ({source})")
    try:
        df = download_df_from_gcs(blob_name)

        with engine.begin() as conn:
            price_rows = parser(df, conn)
            if price_rows:
                _upsert_daily_prices(conn, price_rows)
                _upsert_latest_prices(conn, price_rows)

        count = len(price_rows)
        _mark_status(file_name, FileStatus.SYNCED, rows_synced=count)
        logger.info(f"[SYNC] {file_name} → {count} rows synced")
        return {"file_name": file_name, "status": "synced", "rows_synced": count}

    except Exception as exc:
        logger.error(f"[SYNC] Failed {file_name}: {exc}", exc_info=True)
        _mark_status(file_name, FileStatus.SYNC_FAILED, error=str(exc))
        return {"file_name": file_name, "status": "sync_failed", "rows_synced": 0, "error": str(exc)}


def sync_pending() -> dict:
    """
    Sync all bhavcopy_files with status='downloaded'.
    Returns summary: {total, synced, failed, results: [...]}
    """
    with engine.connect() as conn:
        pending = conn.execute(text("""
            SELECT file_name, trade_date, source
            FROM bhavcopy_files
            WHERE status = :dl_status  -- FileStatus.DOWNLOADED
            ORDER BY trade_date ASC, source ASC
        """), {"dl_status": int(FileStatus.DOWNLOADED)}).fetchall()

    if not pending:
        logger.info("[SYNC] No pending files to sync")
        return {"total": 0, "synced": 0, "failed": 0, "results": []}

    logger.info(f"[SYNC] {len(pending)} pending file(s) to sync")
    results = [sync_file({"file_name": r[0], "trade_date": r[1], "source": r[2]}) for r in pending]

    synced = sum(1 for r in results if r["status"] == "synced")
    failed = sum(1 for r in results if r["status"] == "sync_failed")

    summary = {"total": len(pending), "synced": synced, "failed": failed, "results": results}
    logger.info(f"[SYNC] Complete: {summary}")
    return summary


if __name__ == "__main__":
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO)
    sync_pending()
