"""
EOD price fetcher — triggered by APScheduler at 18:30 IST on trading days.

Sources:
  NSE equities  : jugaad-data (handles NSE session/cookies internally)
  BSE equities  : BSE bhavcopy direct CSV URL
  AMFI MF NAVs  : AMFI daily NAV text file (pipe-separated)

Each fetcher:
  1. Downloads the day's file
  2. Parses rows into (instrument_id, date, OHLCV)
  3. Upserts into daily_prices + latest_prices
  4. After all complete, warms in-memory cache
"""

import io
import logging
import os
import tempfile
from datetime import date

import pandas as pd
import requests
from sqlalchemy import text

from app import cache
from app.database import engine

logger = logging.getLogger(__name__)

_NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

_BSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_trading_day(trade_date: date) -> bool:
    """Skip weekends and known market holidays."""
    if trade_date.weekday() >= 5:
        logger.info(f"{trade_date} is a weekend — skipping EOD fetch")
        return False
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT 1 FROM trading_calendar WHERE holiday_date = :d"),
            {"d": trade_date.isoformat()},
        ).first()
    if row:
        logger.info(f"{trade_date} is a market holiday — skipping EOD fetch")
        return False
    return True


def _isin_to_instrument_id(conn, isins: list[str]) -> dict[str, int]:
    """Return {isin: instrument_id} for all ISINs that exist in our DB."""
    if not isins:
        return {}
    placeholders = ",".join(f":isin_{i}" for i in range(len(isins)))
    params = {f"isin_{i}": isin for i, isin in enumerate(isins)}
    rows = conn.execute(
        text(f"SELECT isin, instrument_id FROM instruments WHERE isin IN ({placeholders})"),
        params,
    ).fetchall()
    return {row[0]: row[1] for row in rows}


def _upsert_daily_prices(conn, rows: list[dict]) -> None:
    """Upsert rows into daily_prices."""
    for row in rows:
        conn.execute(
            text("""
                INSERT INTO daily_prices
                    (instrument_id, trade_date, open_price_paise, high_price_paise,
                     low_price_paise, close_price_paise, volume, source)
                VALUES
                    (:instrument_id, :trade_date, :open, :high, :low, :close, :volume, :source)
                ON CONFLICT(instrument_id, trade_date) DO UPDATE SET
                    open_price_paise  = excluded.open_price_paise,
                    high_price_paise  = excluded.high_price_paise,
                    low_price_paise   = excluded.low_price_paise,
                    close_price_paise = excluded.close_price_paise,
                    volume            = excluded.volume
            """),
            row,
        )


def _upsert_latest_prices(conn, rows: list[dict]) -> None:
    """
    Upsert rows into latest_prices.

    Rows may include optional open/high/low keys.
    last_synced_at is updated only when close_price_paise actually changes
    so clients can use it for incremental syncs.
    """
    for row in rows:
        conn.execute(
            text("""
                INSERT INTO latest_prices
                    (instrument_id, price_date,
                     open_price_paise, high_price_paise, low_price_paise,
                     close_price_paise, last_synced_at, updated_at)
                VALUES
                    (:instrument_id, :trade_date,
                     :open, :high, :low,
                     :close, datetime('now'), datetime('now'))
                ON CONFLICT(instrument_id) DO UPDATE SET
                    price_date        = excluded.price_date,
                    open_price_paise  = COALESCE(excluded.open_price_paise,  open_price_paise),
                    high_price_paise  = COALESCE(excluded.high_price_paise,  high_price_paise),
                    low_price_paise   = COALESCE(excluded.low_price_paise,   low_price_paise),
                    close_price_paise = excluded.close_price_paise,
                    last_synced_at    = CASE
                        WHEN close_price_paise != excluded.close_price_paise
                        THEN datetime('now')
                        ELSE last_synced_at
                    END,
                    updated_at        = datetime('now')
            """),
            {**row, "open": row.get("open"), "high": row.get("high"), "low": row.get("low")},
        )


def _rupees_to_paise(val) -> int:
    """Convert a rupee float/string to integer paise."""
    try:
        return int(round(float(val) * 100))
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# NSE fetcher
# ---------------------------------------------------------------------------

def fetch_nse_eod(trade_date: date) -> int:
    """
    Download NSE equity bhavcopy via jugaad-data, upsert into DB.
    Returns number of instruments updated.
    """
    logger.info(f"[NSE] Fetching bhavcopy for {trade_date}")
    try:
        from jugaad_data.nse import bhavcopy_save

        with tempfile.TemporaryDirectory() as tmp_dir:
            bhavcopy_save(trade_date, tmp_dir)
            files = os.listdir(tmp_dir)
            if not files:
                logger.warning("[NSE] No bhavcopy file downloaded")
                return 0
            csv_path = os.path.join(tmp_dir, files[0])
            df = pd.read_csv(csv_path)

        # Filter EQ series only
        df = df[df["SctySrs"] == "EQ"].copy()
        if df.empty:
            logger.warning("[NSE] No EQ rows in bhavcopy")
            return 0

        logger.info(f"[NSE] {len(df)} EQ rows parsed")

        with engine.begin() as conn:
            isin_map = _isin_to_instrument_id(conn, df["ISIN"].tolist())
            rows = []
            for _, r in df.iterrows():
                isin = r["ISIN"]
                if isin not in isin_map:
                    continue
                rows.append({
                    "instrument_id": isin_map[isin],
                    "trade_date": trade_date.isoformat(),
                    "open":   _rupees_to_paise(r.get("OpnPric")),
                    "high":   _rupees_to_paise(r.get("HghPric")),
                    "low":    _rupees_to_paise(r.get("LwPric")),
                    "close":  _rupees_to_paise(r.get("ClsPric")),
                    "volume": int(r.get("TtlTradgVol", 0) or 0),
                    "source": "NSE",
                })
            _upsert_daily_prices(conn, rows)
            _upsert_latest_prices(conn, rows)

        logger.info(f"[NSE] Upserted {len(rows)} instruments")
        return len(rows)

    except Exception as e:
        logger.error(f"[NSE] Fetch failed: {e}", exc_info=True)
        return 0


# ---------------------------------------------------------------------------
# BSE fetcher
# ---------------------------------------------------------------------------

def fetch_bse_eod(trade_date: date) -> int:
    """
    Download BSE equity bhavcopy directly, upsert into DB.
    Skips ISINs already inserted by NSE fetcher (NSE is authoritative for dual-listed).
    Returns number of instruments updated.
    """
    logger.info(f"[BSE] Fetching bhavcopy for {trade_date}")
    try:
        date_str = trade_date.strftime("%Y%m%d")
        url = (
            f"https://www.bseindia.com/download/BhavCopy/Equity/"
            f"BhavCopy_BSE_CM_0_0_0_{date_str}_F_0000.CSV"
        )
        resp = requests.get(url, headers=_BSE_HEADERS, timeout=60)
        if resp.status_code != 200:
            logger.warning(f"[BSE] HTTP {resp.status_code} for {url}")
            return 0

        df = pd.read_csv(io.StringIO(resp.text))

        # Group A = large/mid cap equities; also include B, T etc. for broader coverage
        equity_series = {"A", "B", "T", "XT", "X", "Z", "M", "MT"}
        df = df[df["SctySrs"].isin(equity_series)].copy()
        if df.empty:
            logger.warning("[BSE] No equity rows in bhavcopy")
            return 0

        logger.info(f"[BSE] {len(df)} equity rows parsed")

        with engine.begin() as conn:
            # Find which ISINs already have NSE prices today (skip those)
            nse_today = set()
            nse_rows = conn.execute(
                text("SELECT i.isin FROM daily_prices dp "
                     "JOIN instruments i ON dp.instrument_id = i.instrument_id "
                     "WHERE dp.trade_date = :d AND dp.source = 'NSE'"),
                {"d": trade_date.isoformat()},
            ).fetchall()
            nse_today = {r[0] for r in nse_rows}

            isin_map = _isin_to_instrument_id(conn, df["ISIN"].tolist())
            rows = []
            for _, r in df.iterrows():
                isin = r["ISIN"]
                if isin not in isin_map or isin in nse_today:
                    continue
                rows.append({
                    "instrument_id": isin_map[isin],
                    "trade_date": trade_date.isoformat(),
                    "open":   _rupees_to_paise(r.get("OpnPric")),
                    "high":   _rupees_to_paise(r.get("HghPric")),
                    "low":    _rupees_to_paise(r.get("LwPric")),
                    "close":  _rupees_to_paise(r.get("ClsPric")),
                    "volume": int(r.get("TtlTradgVol", 0) or 0),
                    "source": "BSE",
                })
            _upsert_daily_prices(conn, rows)
            _upsert_latest_prices(conn, rows)

        logger.info(f"[BSE] Upserted {len(rows)} BSE-only instruments")
        return len(rows)

    except Exception as e:
        logger.error(f"[BSE] Fetch failed: {e}", exc_info=True)
        return 0


# ---------------------------------------------------------------------------
# AMFI NAV fetcher
# ---------------------------------------------------------------------------

def fetch_amfi_nav(trade_date: date) -> int:
    """
    Download AMFI daily NAV file and upsert into nav_history + latest_prices.
    Returns number of schemes updated.

    File format (semicolon-separated):
    Scheme Code;ISIN Div Payout/IDCW;ISIN Div Reinvestment;Scheme Name;NAV;Date
    """
    logger.info(f"[AMFI] Fetching NAV for {trade_date}")
    try:
        url = "https://www.amfiindia.com/spages/NAVAll.txt"
        resp = requests.get(url, timeout=60)
        if resp.status_code != 200:
            logger.warning(f"[AMFI] HTTP {resp.status_code}")
            return 0

        lines = resp.text.splitlines()
        records = []  # (isin, nav_paise, nav_date_str)

        for line in lines:
            line = line.strip()
            if not line or ";" not in line:
                continue
            parts = line.split(";")
            if len(parts) < 6:
                continue
            # Skip header lines
            try:
                float(parts[4])
            except (ValueError, IndexError):
                continue

            # Two ISINs per row — dividend payout and reinvestment
            for isin_col in [1, 2]:
                isin = parts[isin_col].strip() if len(parts) > isin_col else ""
                if not isin or len(isin) != 12:
                    continue
                try:
                    nav_paise = _rupees_to_paise(parts[4].strip())
                    nav_date_str = parts[5].strip()  # DD-Mon-YYYY or DD-MM-YYYY
                    # Normalise to YYYY-MM-DD
                    nav_date = _parse_amfi_date(nav_date_str)
                    if nav_date is None:
                        continue
                    records.append((isin, nav_paise, nav_date))
                except Exception:
                    continue

        if not records:
            logger.warning("[AMFI] No valid NAV records parsed")
            return 0

        logger.info(f"[AMFI] {len(records)} NAV records parsed")

        all_isins = list({r[0] for r in records})
        with engine.begin() as conn:
            isin_map = _isin_to_instrument_id(conn, all_isins)

            nav_rows = []
            latest_rows = []
            seen = set()  # deduplicate (instrument_id, nav_date)

            for isin, nav_paise, nav_date in records:
                if isin not in isin_map:
                    continue
                instrument_id = isin_map[isin]
                key = (instrument_id, nav_date)
                if key in seen:
                    continue
                seen.add(key)

                nav_rows.append({
                    "instrument_id": instrument_id,
                    "nav_date": nav_date,
                    "nav_paise": nav_paise,
                })
                # Update latest_prices only if this NAV is for today or most recent
                latest_rows.append({
                    "instrument_id": instrument_id,
                    "trade_date": nav_date,
                    "close": nav_paise,
                })

            # Upsert nav_history
            for row in nav_rows:
                conn.execute(
                    text("""
                        INSERT INTO nav_history (instrument_id, nav_date, nav_paise)
                        VALUES (:instrument_id, :nav_date, :nav_paise)
                        ON CONFLICT(instrument_id, nav_date) DO UPDATE SET
                            nav_paise = excluded.nav_paise
                    """),
                    row,
                )

            _upsert_latest_prices(conn, latest_rows)

        logger.info(f"[AMFI] Upserted {len(nav_rows)} NAV records")
        return len(nav_rows)

    except Exception as e:
        logger.error(f"[AMFI] Fetch failed: {e}", exc_info=True)
        return 0


def _parse_amfi_date(date_str: str):
    """Parse AMFI date formats: DD-Mon-YYYY or DD-MM-YYYY → YYYY-MM-DD string."""
    import re
    date_str = date_str.strip()
    # Try DD-Mon-YYYY e.g. "09-Apr-2026"
    try:
        from datetime import datetime
        return datetime.strptime(date_str, "%d-%b-%Y").strftime("%Y-%m-%d")
    except ValueError:
        pass
    # Try DD-MM-YYYY
    try:
        from datetime import datetime
        return datetime.strptime(date_str, "%d-%m-%Y").strftime("%Y-%m-%d")
    except ValueError:
        pass
    return None


# ---------------------------------------------------------------------------
# Cache warm
# ---------------------------------------------------------------------------

def warm_cache(trade_date: date) -> None:
    """Load today's closing prices from latest_prices into in-memory cache."""
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT i.isin, lp.close_price_paise
                FROM latest_prices lp
                JOIN instruments i ON lp.instrument_id = i.instrument_id
                WHERE lp.price_date = :d
            """),
            {"d": trade_date.isoformat()},
        ).fetchall()

    prices = {row[0]: row[1] for row in rows}
    cache.set_prices(trade_date, prices)
    logger.info(f"Cache warmed with {len(prices)} prices for {trade_date}")


# ---------------------------------------------------------------------------
# Intraday snapshot  (runs every 30 min during market hours)
# ---------------------------------------------------------------------------

def fetch_intraday_snapshot(trade_date: date) -> int:
    """
    Download NSE's current-day bhavcopy snapshot and refresh latest_prices.

    Only updates last_synced_at when close_price_paise actually changes, so
    clients can use that field for incremental syncs.  OHLC fields are always
    refreshed to reflect the running day's range.

    Returns number of instruments whose close price changed.
    """
    logger.info(f"[INTRADAY] Fetching NSE snapshot for {trade_date}")
    try:
        from jugaad_data.nse import bhavcopy_save

        with tempfile.TemporaryDirectory() as tmp_dir:
            bhavcopy_save(trade_date, tmp_dir)
            files = os.listdir(tmp_dir)
            if not files:
                logger.warning("[INTRADAY] No snapshot file downloaded")
                return 0
            df = pd.read_csv(os.path.join(tmp_dir, files[0]))

        df = df[df["SctySrs"] == "EQ"].copy()
        if df.empty:
            return 0

        logger.info(f"[INTRADAY] {len(df)} EQ rows in snapshot")

        with engine.begin() as conn:
            isin_map = _isin_to_instrument_id(conn, df["ISIN"].tolist())
            rows = []
            for _, r in df.iterrows():
                isin = r["ISIN"]
                if isin not in isin_map:
                    continue
                # During market hours ClsPric may be 0 — fall back to LwPric or previous
                close = _rupees_to_paise(r.get("ClsPric") or r.get("LstTrdPric") or 0)
                if close <= 0:
                    continue
                rows.append({
                    "instrument_id": isin_map[isin],
                    "trade_date":    trade_date.isoformat(),
                    "open":  _rupees_to_paise(r.get("OpnPric")),
                    "high":  _rupees_to_paise(r.get("HghPric")),
                    "low":   _rupees_to_paise(r.get("LwPric")),
                    "close": close,
                })

            # Count how many prices will actually change before upserting
            changed = 0
            for row in rows:
                existing = conn.execute(
                    text("SELECT close_price_paise FROM latest_prices WHERE instrument_id = :iid"),
                    {"iid": row["instrument_id"]},
                ).scalar()
                if existing != row["close"]:
                    changed += 1

            _upsert_latest_prices(conn, rows)

        logger.info(f"[INTRADAY] {changed}/{len(rows)} prices changed")
        return changed

    except Exception as e:
        logger.error(f"[INTRADAY] Fetch failed: {e}", exc_info=True)
        return 0


def intraday_fetch_job() -> None:
    """
    Called by APScheduler every 30 minutes.
    Guards: only runs on weekdays between 09:35 and 15:35 IST.
    """
    import pytz
    from datetime import datetime, time as dtime
    ist = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.now(ist).time()
    if not (dtime(9, 35) <= now_ist <= dtime(15, 35)):
        return
    today = date.today()
    if not is_trading_day(today):
        return
    fetch_intraday_snapshot(today)


# ---------------------------------------------------------------------------
# NSE F&O fetcher
# ---------------------------------------------------------------------------

def fetch_nse_fo_eod(trade_date: date) -> int:
    """
    Download NSE F&O bhavcopy, upsert daily_prices for derivatives instruments.
    Creates instrument + instrument_derivatives records for contracts not yet in DB.
    Returns number of price rows upserted.
    """
    logger.info(f"[NSE-FO] Fetching F&O bhavcopy for {trade_date}")
    try:
        date_str = trade_date.strftime("%Y%m%d")
        url = (
            f"https://archives.nseindia.com/content/fo/"
            f"BhavCopy_NSE_FO_0_0_0_{date_str}_F_0000.csv"
        )
        resp = requests.get(url, headers=_NSE_HEADERS, timeout=60)
        if resp.status_code != 200:
            logger.warning(f"[NSE-FO] HTTP {resp.status_code} for {url}")
            return 0

        df = pd.read_csv(io.StringIO(resp.text))
        logger.info(f"[NSE-FO] {len(df)} F&O rows parsed")

        with engine.begin() as conn:
            futures_type_id: int = conn.execute(text(
                "SELECT instrument_type_id FROM instrument_types WHERE name='FUTURES' LIMIT 1"
            )).scalar()
            options_type_id: int = conn.execute(text(
                "SELECT instrument_type_id FROM instrument_types WHERE name='OPTIONS' LIMIT 1"
            )).scalar()
            nse_exchange_id: int = conn.execute(text(
                "SELECT exchange_id FROM exchanges WHERE code='NSE' LIMIT 1"
            )).scalar()

            rows = []
            created = 0

            for _, r in df.iterrows():
                isin = str(r.get("ISIN", "")).strip()
                if not isin or len(isin) != 12:
                    continue

                # Find existing instrument by contract ISIN
                instrument_id = conn.execute(text(
                    "SELECT instrument_id FROM instruments WHERE isin = :isin"
                ), {"isin": isin}).scalar()

                if instrument_id is None:
                    # Create new derivatives instrument for this contract
                    fin_type  = str(r.get("FinInstrmNm", "")).strip()
                    ticker    = str(r.get("TckrSymb", "")).strip()
                    expiry    = str(r.get("XpryDt", "")).strip()
                    strike    = float(r.get("StrkPric", 0) or 0)
                    opt_type  = str(r.get("OptnTp", "XX")).strip()

                    is_future = fin_type.startswith("FUT")
                    type_id   = futures_type_id if is_future else options_type_id
                    if not type_id:
                        continue

                    name = (f"{ticker} {expiry} FUT" if is_future
                            else f"{ticker} {int(strike)}{opt_type} {expiry}")

                    conn.execute(text("""
                        INSERT OR IGNORE INTO instruments
                            (isin, name, instrument_type_id, primary_exchange_id, source)
                        VALUES (:isin, :name, :type_id, :exch, 'SERVER')
                    """), {"isin": isin, "name": name,
                           "type_id": type_id, "exch": nse_exchange_id})

                    instrument_id = conn.execute(text(
                        "SELECT instrument_id FROM instruments WHERE isin = :isin"
                    ), {"isin": isin}).scalar()

                    if instrument_id is None:
                        continue

                    # Look up underlying by NSE symbol
                    underlying_isin = conn.execute(text("""
                        SELECT i.isin FROM instruments i
                        JOIN instrument_equity ie ON i.instrument_id = ie.instrument_id
                        WHERE ie.nse_symbol = :sym LIMIT 1
                    """), {"sym": ticker}).scalar()

                    conn.execute(text("""
                        INSERT OR IGNORE INTO instrument_derivatives
                            (instrument_id, underlying_isin, expiry_date,
                             lot_size, strike_price_paise, contract_type)
                        VALUES (:iid, :undl, :exp, 1, :strike, :ctype)
                    """), {
                        "iid":    instrument_id,
                        "undl":   underlying_isin,
                        "exp":    expiry,
                        "strike": int(strike * 100),
                        "ctype":  opt_type if opt_type not in ("XX", "-", "") else None,
                    })
                    created += 1

                # Settlement price is the authoritative close for F&O
                close = _rupees_to_paise(
                    r.get("SttlmntPric") or r.get("ClsPric") or 0
                )
                if close <= 0:
                    continue

                rows.append({
                    "instrument_id": instrument_id,
                    "trade_date": trade_date.isoformat(),
                    "open":   _rupees_to_paise(r.get("OpnPric")),
                    "high":   _rupees_to_paise(r.get("HghPric")),
                    "low":    _rupees_to_paise(r.get("LwPric")),
                    "close":  close,
                    "volume": int(r.get("TtlTradgVol", 0) or 0),
                    "source": "NSE",
                })

            _upsert_daily_prices(conn, rows)
            _upsert_latest_prices(conn, rows)

        logger.info(f"[NSE-FO] Created {created} new contracts, upserted {len(rows)} prices")
        return len(rows)

    except Exception as e:
        logger.error(f"[NSE-FO] Fetch failed: {e}", exc_info=True)
        return 0


# ---------------------------------------------------------------------------
# Main entry point — called by APScheduler and admin trigger
# ---------------------------------------------------------------------------

def run_all(trade_date: date | None = None, force: bool = False) -> dict:
    """
    Run all EOD price fetchers.
    force=True bypasses the trading-day check — useful for backfills and
    one-off manual runs.
    """
    if trade_date is None:
        trade_date = date.today()

    if not force and not is_trading_day(trade_date):
        return {"skipped": True, "reason": "non-trading day", "date": trade_date.isoformat()}

    logger.info(f"=== EOD price fetch starting for {trade_date} (force={force}) ===")
    cache.invalidate()

    nse_count  = fetch_nse_eod(trade_date)
    bse_count  = fetch_bse_eod(trade_date)
    amfi_count = fetch_amfi_nav(trade_date)
    fo_count   = fetch_nse_fo_eod(trade_date)
    total      = nse_count + bse_count + amfi_count + fo_count

    warm_cache(trade_date)

    result = {
        "date":  trade_date.isoformat(),
        "nse":   nse_count,
        "bse":   bse_count,
        "amfi":  amfi_count,
        "fo":    fo_count,
        "total": total,
    }
    logger.info(f"=== EOD fetch complete: {result} ===")
    return result


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    force_flag = "--force" in sys.argv
    run_all(force=force_flag)
