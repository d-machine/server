"""
EOD price fetcher — triggered by APScheduler at 18:30 IST on trading days.

NSE equity : nsearchives direct CSV  → BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv
             Columns: SctySrs, ISIN, OpnPric, HghPric, LwPric, ClsPric, TtlTradgVol
             Filter : SctySrs == 'EQ'   Lookup: ISIN
NSE F&O    : nsearchives zip           → BhavCopy_NSE_FO_0_0_0_YYYYMMDD_F_0000.csv.zip
BSE equity : BSE direct CSV            → BhavCopy_BSE_CM_0_0_0_YYYYMMDD_F_0000.CSV
AMFI NAV   : AMFI NAVAll.txt
"""

import io
import logging
import zipfile
from datetime import date

import pandas as pd
import requests
from sqlalchemy import text

from app import cache
from app.database import engine

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_trading_day(d: date) -> bool:
    if d.weekday() >= 5:
        logger.info(f"{d} is a weekend")
        return False
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT 1 FROM trading_calendar WHERE trade_date=:d AND is_trading_day=0"),
            {"d": d.isoformat()},
        ).first()
    if row:
        logger.info(f"{d} is a market holiday")
        return False
    return True


def _isin_map(conn, isins):
    """Look up instrument_id by ISIN via instrument_equity (isin moved out of hub)."""
    if not isins:
        return {}
    ph = ",".join(f":i{n}" for n in range(len(isins)))
    rows = conn.execute(
        text(f"SELECT isin, instrument_id FROM instrument_equity WHERE isin IN ({ph})"),
        {f"i{n}": v for n, v in enumerate(isins)},
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def _upsert_daily(conn, rows):
    for row in rows:
        conn.execute(text("""
            INSERT INTO daily_prices
                (instrument_id,trade_date,open_price_paise,high_price_paise,
                 low_price_paise,close_price_paise,volume,source)
            VALUES (:instrument_id,:trade_date,:open,:high,:low,:close,:volume,:source)
            ON CONFLICT(instrument_id,trade_date) DO UPDATE SET
                open_price_paise=excluded.open_price_paise,
                high_price_paise=excluded.high_price_paise,
                low_price_paise=excluded.low_price_paise,
                close_price_paise=excluded.close_price_paise,
                volume=excluded.volume
        """), row)


def _upsert_latest(conn, rows):
    for row in rows:
        conn.execute(text("""
            INSERT INTO latest_prices
                (instrument_id,price_date,open_price_paise,high_price_paise,
                 low_price_paise,close_price_paise,last_synced_at,updated_at)
            VALUES (:instrument_id,:trade_date,:open,:high,:low,:close,
                    datetime('now'),datetime('now'))
            ON CONFLICT(instrument_id) DO UPDATE SET
                price_date=excluded.price_date,
                open_price_paise=COALESCE(excluded.open_price_paise,open_price_paise),
                high_price_paise=COALESCE(excluded.high_price_paise,high_price_paise),
                low_price_paise=COALESCE(excluded.low_price_paise,low_price_paise),
                close_price_paise=excluded.close_price_paise,
                last_synced_at=CASE
                    WHEN close_price_paise!=excluded.close_price_paise
                    THEN datetime('now') ELSE last_synced_at END,
                updated_at=datetime('now')
        """), {**row, "open": row.get("open"), "high": row.get("high"), "low": row.get("low")})


def _to_paise(val):
    try:
        return int(round(float(val) * 100))
    except (TypeError, ValueError):
        return 0


def _nse_session():
    s = requests.Session()
    try:
        s.get("https://www.nseindia.com", headers=_NSE_HEADERS, timeout=15)
    except Exception:
        pass
    return s


def _read_csv_or_zip(content: bytes) -> pd.DataFrame:
    """Read CSV bytes — transparently handles zip-wrapped CSVs."""
    if content[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            csv_name = next((n for n in zf.namelist() if n.lower().endswith(".csv")), None)
            if not csv_name:
                raise ValueError(f"No CSV in zip. Files: {zf.namelist()}")
            return pd.read_csv(zf.open(csv_name))
    return pd.read_csv(io.BytesIO(content))


# ---------------------------------------------------------------------------
# NSE equity fetcher
# ---------------------------------------------------------------------------

def fetch_nse_eod(trade_date: date) -> int:
    """
    Download NSE CM bhavcopy from nsearchives, filter SctySrs='EQ', upsert by ISIN.
    URL: https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv
    """
    logger.info(f"[NSE] Fetching for {trade_date}")
    try:
        date_str = trade_date.strftime("%Y%m%d")
        url = (f"https://nsearchives.nseindia.com/content/cm/"
               f"BhavCopy_NSE_CM_0_0_0_{date_str}_F_0000.csv.zip")
        resp = _nse_session().get(url, headers=_NSE_HEADERS, timeout=60)
        if resp.status_code != 200:
            logger.warning(f"[NSE] HTTP {resp.status_code} for {url}")
            return 0

        df = _read_csv_or_zip(resp.content)
        df = df[df["SctySrs"].astype(str).str.strip() == "EQ"].copy()
        if df.empty:
            logger.warning("[NSE] No EQ rows")
            return 0
        logger.info(f"[NSE] {len(df)} EQ rows")

        with engine.begin() as conn:
            im = _isin_map(conn, df["ISIN"].tolist())
            rows = []
            for _, r in df.iterrows():
                isin = r["ISIN"]
                if isin not in im:
                    continue
                rows.append({
                    "instrument_id": im[isin],
                    "trade_date": trade_date.isoformat(),
                    "open":   _to_paise(r.get("OpnPric")),
                    "high":   _to_paise(r.get("HghPric")),
                    "low":    _to_paise(r.get("LwPric")),
                    "close":  _to_paise(r.get("ClsPric")),
                    "volume": int(r.get("TtlTradgVol", 0) or 0),
                    "source": "NSE",
                })
            _upsert_daily(conn, rows)
            _upsert_latest(conn, rows)
        logger.info(f"[NSE] Upserted {len(rows)}")
        return len(rows)
    except Exception as e:
        logger.error(f"[NSE] Failed: {e}", exc_info=True)
        return 0


# ---------------------------------------------------------------------------
# BSE equity fetcher
# ---------------------------------------------------------------------------

def fetch_bse_eod(trade_date: date) -> int:
    """BSE CM bhavcopy — skips ISINs already priced by NSE."""
    logger.info(f"[BSE] Fetching for {trade_date}")
    try:
        date_str = trade_date.strftime("%Y%m%d")
        url = (f"https://www.bseindia.com/download/BhavCopy/Equity/"
               f"BhavCopy_BSE_CM_0_0_0_{date_str}_F_0000.CSV")
        resp = requests.get(url, headers=_BSE_HEADERS, timeout=60)
        if resp.status_code != 200:
            logger.warning(f"[BSE] HTTP {resp.status_code}")
            return 0

        df = _read_csv_or_zip(resp.content)
        eq_series = {"A", "B", "T", "XT", "X", "Z", "M", "MT"}
        df = df[df["SctySrs"].isin(eq_series)].copy()
        if df.empty:
            logger.warning("[BSE] No equity rows")
            return 0
        logger.info(f"[BSE] {len(df)} rows")

        with engine.begin() as conn:
            nse_isins = {r[0] for r in conn.execute(
                text("SELECT i.isin FROM daily_prices dp "
                     "JOIN instruments i ON dp.instrument_id=i.instrument_id "
                     "WHERE dp.trade_date=:d AND dp.source='NSE'"),
                {"d": trade_date.isoformat()},
            ).fetchall()}
            im = _isin_map(conn, df["ISIN"].tolist())
            rows = []
            for _, r in df.iterrows():
                isin = r["ISIN"]
                if isin not in im or isin in nse_isins:
                    continue
                rows.append({
                    "instrument_id": im[isin],
                    "trade_date": trade_date.isoformat(),
                    "open":   _to_paise(r.get("OpnPric")),
                    "high":   _to_paise(r.get("HghPric")),
                    "low":    _to_paise(r.get("LwPric")),
                    "close":  _to_paise(r.get("ClsPric")),
                    "volume": int(r.get("TtlTradgVol", 0) or 0),
                    "source": "BSE",
                })
            _upsert_daily(conn, rows)
            _upsert_latest(conn, rows)
        logger.info(f"[BSE] Upserted {len(rows)} BSE-only")
        return len(rows)
    except Exception as e:
        logger.error(f"[BSE] Failed: {e}", exc_info=True)
        return 0


# ---------------------------------------------------------------------------
# AMFI NAV fetcher
# ---------------------------------------------------------------------------

def _parse_amfi_date(s):
    s = s.strip()
    for fmt in ("%d-%b-%Y", "%d-%m-%Y"):
        try:
            from datetime import datetime as dt
            return dt.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def fetch_amfi_nav(trade_date: date) -> int:
    logger.info(f"[AMFI] Fetching NAV for {trade_date}")
    try:
        resp = requests.get("https://www.amfiindia.com/spages/NAVAll.txt", timeout=60)
        if resp.status_code != 200:
            logger.warning(f"[AMFI] HTTP {resp.status_code}")
            return 0

        records = []
        for line in resp.text.splitlines():
            line = line.strip()
            if not line or ";" not in line:
                continue
            parts = line.split(";")
            if len(parts) < 6:
                continue
            try:
                float(parts[4])
            except (ValueError, IndexError):
                continue
            for col in [1, 2]:
                isin = parts[col].strip() if len(parts) > col else ""
                if len(isin) != 12:
                    continue
                try:
                    nav_paise = _to_paise(parts[4].strip())
                    nav_date  = _parse_amfi_date(parts[5])
                    if nav_date:
                        records.append((isin, nav_paise, nav_date))
                except Exception:
                    pass

        if not records:
            logger.warning("[AMFI] No valid records")
            return 0
        logger.info(f"[AMFI] {len(records)} records")

        with engine.begin() as conn:
            im = _isin_map(conn, list({r[0] for r in records}))
            seen = set()
            nav_rows, lat_rows = [], []
            for isin, nav_paise, nav_date in records:
                if isin not in im:
                    continue
                iid = im[isin]
                key = (iid, nav_date)
                if key in seen:
                    continue
                seen.add(key)
                nav_rows.append({"instrument_id": iid, "nav_date": nav_date, "nav_paise": nav_paise})
                lat_rows.append({"instrument_id": iid, "trade_date": nav_date, "close": nav_paise})

            for row in nav_rows:
                conn.execute(text("""
                    INSERT INTO nav_history (instrument_id,nav_date,nav_paise)
                    VALUES (:instrument_id,:nav_date,:nav_paise)
                    ON CONFLICT(instrument_id,nav_date) DO UPDATE SET
                        nav_paise=excluded.nav_paise
                """), row)
            _upsert_latest(conn, lat_rows)

        logger.info(f"[AMFI] Upserted {len(nav_rows)}")
        return len(nav_rows)
    except Exception as e:
        logger.error(f"[AMFI] Failed: {e}", exc_info=True)
        return 0


# ---------------------------------------------------------------------------
# Cache warm
# ---------------------------------------------------------------------------

def warm_cache(trade_date: date) -> None:
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT lp.instrument_id, lp.close_price_paise
                FROM latest_prices lp
                WHERE lp.price_date=:d
            """), {"d": trade_date.isoformat()}).fetchall()
        cache.set_prices(trade_date, {r[0]: r[1] for r in rows})
        logger.info(f"Cache warmed: {len(rows)} prices for {trade_date}")
    except Exception as e:
        logger.warning(f"Cache warm failed (non-fatal): {e}")
        cache.set_prices(trade_date, {})


# ---------------------------------------------------------------------------
# Intraday snapshot
# ---------------------------------------------------------------------------

def fetch_intraday_snapshot(trade_date: date) -> int:
    """Same URL as EOD — file may not be available until after market close."""
    logger.info(f"[INTRADAY] Snapshot for {trade_date}")
    try:
        date_str = trade_date.strftime("%Y%m%d")
        url = (f"https://nsearchives.nseindia.com/content/cm/"
               f"BhavCopy_NSE_CM_0_0_0_{date_str}_F_0000.csv.zip")
        resp = _nse_session().get(url, headers=_NSE_HEADERS, timeout=60)
        if resp.status_code != 200:
            logger.warning(f"[INTRADAY] HTTP {resp.status_code} — not yet available")
            return 0

        df = _read_csv_or_zip(resp.content)
        df = df[df["SctySrs"].astype(str).str.strip() == "EQ"].copy()
        if df.empty:
            return 0

        with engine.begin() as conn:
            im = _isin_map(conn, df["ISIN"].tolist())
            rows = []
            for _, r in df.iterrows():
                isin  = r["ISIN"]
                close = _to_paise(r.get("ClsPric"))
                if isin not in im or close <= 0:
                    continue
                rows.append({
                    "instrument_id": im[isin],
                    "trade_date":    trade_date.isoformat(),
                    "open":  _to_paise(r.get("OpnPric")),
                    "high":  _to_paise(r.get("HghPric")),
                    "low":   _to_paise(r.get("LwPric")),
                    "close": close,
                })
            changed = sum(
                1 for row in rows
                if conn.execute(
                    text("SELECT close_price_paise FROM latest_prices WHERE instrument_id=:iid"),
                    {"iid": row["instrument_id"]},
                ).scalar() != row["close"]
            )
            _upsert_latest(conn, rows)
        logger.info(f"[INTRADAY] {changed}/{len(rows)} changed")
        return changed
    except Exception as e:
        logger.error(f"[INTRADAY] Failed: {e}", exc_info=True)
        return 0


def intraday_fetch_job() -> None:
    import pytz
    from datetime import datetime, time as dtime
    ist = pytz.timezone("Asia/Kolkata")
    if not (dtime(9, 35) <= datetime.now(ist).time() <= dtime(15, 35)):
        return
    today = date.today()
    if is_trading_day(today):
        fetch_intraday_snapshot(today)


# ---------------------------------------------------------------------------
# NSE F&O fetcher
# ---------------------------------------------------------------------------

def fetch_nse_fo_eod(trade_date: date) -> int:
    """NSE F&O bhavcopy zip — ISIN-based, creates new derivative instruments as needed."""
    logger.info(f"[NSE-FO] Fetching for {trade_date}")
    try:
        date_str = trade_date.strftime("%Y%m%d")
        url = (f"https://nsearchives.nseindia.com/content/fo/"
               f"BhavCopy_NSE_FO_0_0_0_{date_str}_F_0000.csv.zip")
        resp = _nse_session().get(url, headers=_NSE_HEADERS, timeout=60)
        if resp.status_code != 200:
            logger.warning(f"[NSE-FO] HTTP {resp.status_code}")
            return 0

        df = _read_csv_or_zip(resp.content)
        logger.info(f"[NSE-FO] {len(df)} rows")

        with engine.begin() as conn:
            fut_tid = conn.execute(text(
                "SELECT instrument_type_id FROM instrument_types WHERE name='FUTURES' LIMIT 1"
            )).scalar()
            opt_tid = conn.execute(text(
                "SELECT instrument_type_id FROM instrument_types WHERE name='OPTIONS' LIMIT 1"
            )).scalar()
            nse_exch = conn.execute(text(
                "SELECT exchange_id FROM exchanges WHERE code='NSE' LIMIT 1"
            )).scalar()

            rows = []
            created = 0
            for _, r in df.iterrows():
                isin = str(r.get("ISIN", "")).strip()
                if not isin or len(isin) != 12:
                    continue

                iid = conn.execute(text(
                    "SELECT instrument_id FROM instruments WHERE isin=:isin"
                ), {"isin": isin}).scalar()

                if iid is None:
                    fin_type = str(r.get("FinInstrmNm", "")).strip()
                    ticker   = str(r.get("TckrSymb", "")).strip()
                    expiry   = str(r.get("XpryDt", "")).strip()
                    strike   = float(r.get("StrkPric", 0) or 0)
                    opt_type = str(r.get("OptnTp", "XX")).strip()
                    is_fut   = fin_type.startswith("FUT")
                    tid      = fut_tid if is_fut else opt_tid
                    if not tid:
                        continue
                    name = (f"{ticker} {expiry} FUT" if is_fut
                            else f"{ticker} {int(strike)}{opt_type} {expiry}")
                    conn.execute(text("""
                        INSERT OR IGNORE INTO instruments
                            (isin,name,instrument_type_id,primary_exchange_id,source)
                        VALUES (:isin,:name,:tid,:exch,'SERVER')
                    """), {"isin": isin, "name": name, "tid": tid, "exch": nse_exch})
                    iid = conn.execute(text(
                        "SELECT instrument_id FROM instruments WHERE isin=:isin"
                    ), {"isin": isin}).scalar()
                    if iid is None:
                        continue
                    undl = conn.execute(text("""
                        SELECT i.isin FROM instruments i
                        JOIN instrument_equity ie ON i.instrument_id=ie.instrument_id
                        WHERE ie.nse_symbol=:sym LIMIT 1
                    """), {"sym": ticker}).scalar()
                    conn.execute(text("""
                        INSERT OR IGNORE INTO instrument_derivatives
                            (instrument_id,underlying_isin,expiry_date,
                             lot_size,strike_price_paise,contract_type)
                        VALUES (:iid,:undl,:exp,1,:strike,:ctype)
                    """), {
                        "iid": iid, "undl": undl, "exp": expiry,
                        "strike": int(strike * 100),
                        "ctype": opt_type if opt_type not in ("XX", "-", "") else None,
                    })
                    created += 1

                close = _to_paise(r.get("SttlmntPric") or r.get("ClsPric") or 0)
                if close <= 0:
                    continue
                rows.append({
                    "instrument_id": iid,
                    "trade_date": trade_date.isoformat(),
                    "open":   _to_paise(r.get("OpnPric")),
                    "high":   _to_paise(r.get("HghPric")),
                    "low":    _to_paise(r.get("LwPric")),
                    "close":  close,
                    "volume": int(r.get("TtlTradgVol", 0) or 0),
                    "source": "NSE",
                })

            _upsert_daily(conn, rows)
            _upsert_latest(conn, rows)

        logger.info(f"[NSE-FO] Created {created}, upserted {len(rows)}")
        return len(rows)
    except Exception as e:
        logger.error(f"[NSE-FO] Failed: {e}", exc_info=True)
        return 0


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_all(trade_date: date | None = None, force: bool = False) -> dict:
    if trade_date is None:
        trade_date = date.today()
    if not force and not is_trading_day(trade_date):
        return {"skipped": True, "reason": "non-trading day", "date": trade_date.isoformat()}

    logger.info(f"=== EOD fetch {trade_date} force={force} ===")
    cache.invalidate()
    nse  = fetch_nse_eod(trade_date)
    bse  = fetch_bse_eod(trade_date)
    amfi = fetch_amfi_nav(trade_date)
    fo   = fetch_nse_fo_eod(trade_date)
    warm_cache(trade_date)
    result = {
        "date": trade_date.isoformat(),
        "nse_eq": nse,
        "bse_eq": bse,
        "amfi_nav": amfi,
        "nse_fo": fo,
    }
    logger.info(f"=== EOD fetch complete: {result} ===")
    return result
