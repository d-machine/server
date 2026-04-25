"""
Instrument master sync.

Downloads reference data from NSE, BSE, and AMFI to build a comprehensive
instrument catalog with ISIN ↔ NSE symbol ↔ BSE code ↔ AMFI code mappings.

Sources:
  NSE equity master  : archives.nseindia.com/content/equities/EQUITY_L.csv
                       → instruments + instrument_equity (isin, nse_symbol, face_value)
  BSE cross-reference: most recent BSE bhavcopy
                       → adds bse_code to instruments already known by ISIN
  AMFI scheme master : amfiindia.com/spages/NAVAll.txt
                       → instruments + instrument_mf (isin, amfi_code, fund_house, etc.)

Run once on first boot (called from lifespan startup), then weekly via scheduler.
"""

import io
import logging
from datetime import date, timedelta

import pandas as pd
import requests
from sqlalchemy import text

from app.database import engine

logger = logging.getLogger(__name__)

NSE_EQUITY_MASTER_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"

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
# NSE equity master
# ---------------------------------------------------------------------------

def populate_nse_equity() -> int:
    """
    Download NSE EQUITY_L.csv and upsert all EQ-series instruments.
    Creates instrument + instrument_equity row with isin, nse_symbol, face_value.
    Returns number of instruments upserted.
    """
    logger.info("[POPULATE-NSE] Downloading NSE equity master")
    try:
        resp = requests.get(NSE_EQUITY_MASTER_URL, headers=_NSE_HEADERS, timeout=60)
        if resp.status_code != 200:
            logger.warning(f"[POPULATE-NSE] HTTP {resp.status_code}")
            return 0

        df = pd.read_csv(io.StringIO(resp.text))
        df.columns = [c.strip() for c in df.columns]

        # Keep only EQ series (delivery equity — excludes SME, BE, etc.)
        if "SERIES" in df.columns:
            df = df[df["SERIES"].str.strip() == "EQ"].copy()

        logger.info(f"[POPULATE-NSE] {len(df)} EQ instruments in master")

        with engine.begin() as conn:
            equity_type_id: int = conn.execute(text(
                "SELECT instrument_type_id FROM instrument_types WHERE name='EQUITY' LIMIT 1"
            )).scalar() or 1
            nse_exchange_id: int = conn.execute(text(
                "SELECT exchange_id FROM exchanges WHERE code='NSE' LIMIT 1"
            )).scalar()

            upserted = 0
            for _, r in df.iterrows():
                isin   = str(r.get("ISIN NUMBER", "")).strip()
                symbol = str(r.get("SYMBOL", "")).strip()
                name   = str(r.get("NAME OF COMPANY", "")).strip()

                if not isin or len(isin) != 12 or not symbol:
                    continue

                try:
                    face_value_paise = int(float(str(r.get("FACE VALUE", 0)).strip()) * 100)
                except (ValueError, TypeError):
                    face_value_paise = None

                # Upsert instrument — update name if already exists
                conn.execute(text("""
                    INSERT INTO instruments
                        (isin, name, instrument_type_id, primary_exchange_id, source)
                    VALUES (:isin, :name, :type_id, :exch, 'SERVER')
                    ON CONFLICT(isin) DO UPDATE SET
                        name       = excluded.name,
                        updated_at = datetime('now')
                """), {"isin": isin, "name": name,
                       "type_id": equity_type_id, "exch": nse_exchange_id})

                instrument_id = conn.execute(text(
                    "SELECT instrument_id FROM instruments WHERE isin = :isin"
                ), {"isin": isin}).scalar()

                if instrument_id is None:
                    continue

                # Upsert instrument_equity — preserve bse_code if already set
                conn.execute(text("""
                    INSERT INTO instrument_equity
                        (instrument_id, nse_symbol, face_value_paise)
                    VALUES (:iid, :sym, :fv)
                    ON CONFLICT(instrument_id) DO UPDATE SET
                        nse_symbol       = COALESCE(:sym, nse_symbol),
                        face_value_paise = COALESCE(:fv,  face_value_paise)
                """), {"iid": instrument_id, "sym": symbol, "fv": face_value_paise})

                upserted += 1

        logger.info(f"[POPULATE-NSE] {upserted} instruments upserted")
        return upserted

    except Exception as e:
        logger.error(f"[POPULATE-NSE] Failed: {e}", exc_info=True)
        return 0


# ---------------------------------------------------------------------------
# BSE cross-reference  (adds bse_code to instruments already known by ISIN)
# ---------------------------------------------------------------------------

def populate_bse_codes() -> int:
    """
    Download the most recent BSE equity bhavcopy and fill in bse_code for
    instruments that already exist in the DB (matched by ISIN).
    Returns number of bse_code values written.
    """
    logger.info("[POPULATE-BSE] Downloading BSE bhavcopy for BSE-code cross-reference")
    try:
        df = _fetch_recent_bse_bhavcopy()
        if df is None:
            logger.warning("[POPULATE-BSE] Could not find a recent BSE bhavcopy")
            return 0

        if "ISIN" not in df.columns or "FinInstrmId" not in df.columns:
            logger.warning(f"[POPULATE-BSE] Unexpected columns: {df.columns.tolist()}")
            return 0

        equity_series = {"A", "B", "T", "XT", "X", "Z", "M", "MT"}
        if "SctySrs" in df.columns:
            df = df[df["SctySrs"].isin(equity_series)].copy()

        with engine.begin() as conn:
            equity_type_id: int = conn.execute(text(
                "SELECT instrument_type_id FROM instrument_types WHERE name='EQUITY' LIMIT 1"
            )).scalar() or 1
            bse_exchange_id: int = conn.execute(text(
                "SELECT exchange_id FROM exchanges WHERE code='BSE' LIMIT 1"
            )).scalar()

            updated = 0
            created = 0
            for _, r in df.iterrows():
                isin     = str(r.get("ISIN", "")).strip()
                bse_code = str(r.get("FinInstrmId", "")).strip()
                name     = str(r.get("ScripNm", r.get("FinInstrmNm", ""))).strip()
                if not isin or len(isin) != 12 or not bse_code:
                    continue

                # Create instrument if not already present (BSE-only stocks)
                existing = conn.execute(text(
                    "SELECT instrument_id FROM instruments WHERE isin = :isin"
                ), {"isin": isin}).scalar()

                if existing is None and name:
                    conn.execute(text("""
                        INSERT OR IGNORE INTO instruments
                            (isin, name, instrument_type_id, primary_exchange_id, source)
                        VALUES (:isin, :name, :type_id, :exch, 'SERVER')
                    """), {"isin": isin, "name": name,
                           "type_id": equity_type_id, "exch": bse_exchange_id})
                    created += 1

                # Upsert bse_code into instrument_equity
                conn.execute(text("""
                    INSERT INTO instrument_equity (instrument_id, bse_code)
                    SELECT instrument_id, :bse_code FROM instruments WHERE isin = :isin
                    ON CONFLICT(instrument_id) DO UPDATE SET
                        bse_code = COALESCE(bse_code, excluded.bse_code)
                """), {"bse_code": bse_code, "isin": isin})

                updated += 1

        logger.info(f"[POPULATE-BSE] {created} instruments created, {updated} bse_codes written")
        return updated

    except Exception as e:
        logger.error(f"[POPULATE-BSE] Failed: {e}", exc_info=True)
        return 0


def _fetch_recent_bse_bhavcopy(max_lookback: int = 5) -> pd.DataFrame | None:
    """Try the last `max_lookback` weekdays to find a downloadable BSE bhavcopy."""
    today = date.today()
    for delta in range(max_lookback):
        check_date = today - timedelta(days=delta)
        if check_date.weekday() >= 5:   # skip weekends
            continue
        date_str = check_date.strftime("%Y%m%d")
        url = (
            f"https://www.bseindia.com/download/BhavCopy/Equity/"
            f"BhavCopy_BSE_CM_0_0_0_{date_str}_F_0000.CSV"
        )
        resp = requests.get(url, headers=_BSE_HEADERS, timeout=60)
        if resp.status_code == 200:
            logger.info(f"[POPULATE-BSE] Using bhavcopy for {check_date}")
            return pd.read_csv(io.StringIO(resp.text))
    return None


# ---------------------------------------------------------------------------
# AMFI scheme master  (MF instruments)
# ---------------------------------------------------------------------------

def populate_amfi_schemes() -> int:
    """
    Download AMFI NAVAll.txt and create MF instruments for every scheme
    that has a valid ISIN.  Populates instruments + instrument_mf.
    Returns number of schemes upserted.
    """
    logger.info("[POPULATE-AMFI] Downloading AMFI scheme master")
    try:
        resp = requests.get("https://www.amfiindia.com/spages/NAVAll.txt", timeout=60)
        if resp.status_code != 200:
            logger.warning(f"[POPULATE-AMFI] HTTP {resp.status_code}")
            return 0

        # File format (semicolon-separated):
        # Scheme Code;ISIN Div Payout/IDCW;ISIN Div Reinvestment;Scheme Name;NAV;Date
        # Header lines (fund house name) have no semicolons or < 4 fields.
        records: list[dict] = []
        current_fund_house = ""

        for line in resp.text.splitlines():
            line = line.strip()
            if not line:
                continue

            # Fund house header lines have no semicolons
            if ";" not in line:
                current_fund_house = line
                continue

            parts = [p.strip() for p in line.split(";")]
            if len(parts) < 4:
                continue

            # Skip column header rows
            try:
                float(parts[4]) if len(parts) > 4 else None
            except (ValueError, IndexError):
                continue  # not a data row

            amfi_code   = parts[0]
            isin_payout = parts[1] if len(parts) > 1 and len(parts[1]) == 12 else None
            isin_reinv  = parts[2] if len(parts) > 2 and len(parts[2]) == 12 else None
            scheme_name = parts[3]

            if not amfi_code or not scheme_name:
                continue

            # Determine plan/option from scheme name
            name_lc = scheme_name.lower()
            plan   = "DIRECT"  if "direct"  in name_lc else "REGULAR"
            option = "GROWTH"  if "growth"  in name_lc else (
                     "IDCW"    if ("idcw" in name_lc or "dividend" in name_lc) else "GROWTH"
            )

            # Determine scheme type from fund house / scheme name
            scheme_type = _infer_mf_type(name_lc)

            for isin in filter(None, [isin_payout, isin_reinv]):
                records.append({
                    "amfi_code":   amfi_code,
                    "isin":        isin,
                    "name":        scheme_name,
                    "fund_house":  current_fund_house,
                    "plan":        plan,
                    "option":      option,
                    "scheme_type": scheme_type,
                })

        logger.info(f"[POPULATE-AMFI] {len(records)} scheme-ISIN pairs parsed")

        with engine.begin() as conn:
            # Resolve instrument_type_ids
            type_ids: dict[str, int] = {}
            for type_name in ("EQUITY_MF", "DEBT_MF", "HYBRID_MF", "ELSS"):
                row = conn.execute(text(
                    "SELECT instrument_type_id FROM instrument_types WHERE name=:n LIMIT 1"
                ), {"n": type_name}).scalar()
                if row:
                    type_ids[type_name] = row

            default_mf_type_id = type_ids.get("EQUITY_MF", 2)

            upserted = 0
            for rec in records:
                type_id = type_ids.get(rec["scheme_type"], default_mf_type_id)

                conn.execute(text("""
                    INSERT INTO instruments
                        (isin, name, instrument_type_id, source)
                    VALUES (:isin, :name, :type_id, 'SERVER')
                    ON CONFLICT(isin) DO UPDATE SET
                        name       = excluded.name,
                        updated_at = datetime('now')
                """), {"isin": rec["isin"], "name": rec["name"], "type_id": type_id})

                instrument_id = conn.execute(text(
                    "SELECT instrument_id FROM instruments WHERE isin = :isin"
                ), {"isin": rec["isin"]}).scalar()

                if instrument_id is None:
                    continue

                conn.execute(text("""
                    INSERT OR IGNORE INTO instrument_mf
                        (instrument_id, amfi_code, scheme_type, fund_house, plan, option)
                    VALUES (:iid, :amfi, :stype, :fh, :plan, :opt)
                """), {
                    "iid": instrument_id, "amfi": rec["amfi_code"],
                    "stype": rec["scheme_type"], "fh": rec["fund_house"],
                    "plan": rec["plan"], "opt": rec["option"],
                })

                upserted += 1

        logger.info(f"[POPULATE-AMFI] {upserted} MF schemes upserted")
        return upserted

    except Exception as e:
        logger.error(f"[POPULATE-AMFI] Failed: {e}", exc_info=True)
        return 0


def _infer_mf_type(name_lc: str) -> str:
    if "elss" in name_lc or "tax saver" in name_lc or "tax saving" in name_lc:
        return "ELSS"
    if any(w in name_lc for w in ("debt", "liquid", "overnight", "money market",
                                   "ultra short", "low duration", "short duration",
                                   "medium duration", "long duration", "gilt",
                                   "credit risk", "corporate bond", "banking and psu",
                                   "floater")):
        return "DEBT_MF"
    if any(w in name_lc for w in ("hybrid", "balanced", "aggressive", "conservative",
                                   "multi asset", "arbitrage", "equity savings",
                                   "dynamic asset")):
        return "HYBRID_MF"
    return "EQUITY_MF"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_all() -> dict:
    """Run all three populate steps and return counts."""
    logger.info("=== Instrument master population starting ===")
    nse   = populate_nse_equity()
    bse   = populate_bse_codes()
    amfi  = populate_amfi_schemes()
    result = {"nse_equity": nse, "bse_codes": bse, "amfi_schemes": amfi}
    logger.info(f"=== Instrument master population complete: {result} ===")
    return result


if __name__ == "__main__":
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO)
    run_all()
