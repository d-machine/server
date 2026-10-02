"""
Shared utilities for all bhavcopy sync parsers.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import pandas as pd
from sqlalchemy import text

from arthdesk_instruments import dao
from app.database import engine
from app.cron.bhavcopy.common import (
    gcs_blob_name, download_df_from_gcs, download_bytes_from_gcs,
    download_df_chunks_from_gcs,
)
from app.cron.bhavcopy.constants import FileStatus

logger = logging.getLogger(__name__)

# Module-level cache: instrument type name → instrument_type_id (server IDs)
_type_id_cache: dict[str, int] = {}


def _get_type_id(name: str) -> Optional[int]:
    """Lookup instrument_type_id by name, with module-level cache."""
    if name not in _type_id_cache:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT instrument_type_id FROM instrument_types WHERE name = :n"),
                {"n": name}
            ).first()
        if row:
            _type_id_cache[name] = row[0]
    return _type_id_cache.get(name)


# -- File tracking ------------------------------------------------------------

def get_pending_files(source: str) -> list[dict]:
    """Return all bhavcopy_files with status=DOWNLOADED for given source."""
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT id, file_name, trade_date
            FROM bhavcopy_files
            WHERE source = :src AND status = :status
            ORDER BY trade_date ASC
        """), {"src": source, "status": int(FileStatus.DOWNLOADED)}).fetchall()
    return [{"id": r[0], "file_name": r[1], "trade_date": r[2]} for r in rows]


def mark_synced(file_name: str, rows_synced: int):
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE bhavcopy_files
            SET status=:status, rows_synced=:rows, error=NULL,
                updated_at=datetime('now')
            WHERE file_name=:fn
        """), {"fn": file_name, "rows": rows_synced, "status": int(FileStatus.SYNCED)})


def mark_failed(file_name: str, error: str):
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE bhavcopy_files
            SET status=:status, error=:err, updated_at=datetime('now')
            WHERE file_name=:fn
        """), {"fn": file_name, "err": str(error)[:2000], "status": int(FileStatus.SYNC_FAILED)})


def load_file_df(trade_date_str: str, file_name: str, **read_csv_kwargs) -> pd.DataFrame:
    trade_date = datetime.strptime(trade_date_str, "%Y-%m-%d").date()
    blob = gcs_blob_name(trade_date, file_name)
    return download_df_from_gcs(blob, **read_csv_kwargs)


def load_file_bytes(trade_date_str: str, file_name: str) -> bytes:
    trade_date = datetime.strptime(trade_date_str, "%Y-%m-%d").date()
    blob = gcs_blob_name(trade_date, file_name)
    return download_bytes_from_gcs(blob)


def load_file_chunks(trade_date_str: str, file_name: str, chunksize: int = 5_000):
    """Return a chunked CSV reader — each iteration yields one DataFrame slice."""
    trade_date = datetime.strptime(trade_date_str, "%Y-%m-%d").date()
    blob = gcs_blob_name(trade_date, file_name)
    return download_df_chunks_from_gcs(blob, chunksize=chunksize)


# -- Type coercion ------------------------------------------------------------

def to_paise(val) -> Optional[int]:
    """Rupee float -> paise integer. Returns None for NaN/None/empty."""
    try:
        if val is None or val == "" or (isinstance(val, float) and pd.isna(val)):
            return None
        f = float(val)
        if pd.isna(f):
            return None
        return int(round(f * 100))
    except (ValueError, TypeError):
        return None


def to_int(val) -> Optional[int]:
    try:
        if val is None or val == "" or (isinstance(val, float) and pd.isna(val)):
            return None
        return int(float(val))
    except (ValueError, TypeError):
        return None


def to_float(val) -> Optional[float]:
    try:
        if val is None or val == "" or (isinstance(val, float) and pd.isna(val)):
            return None
        f = float(val)
        return None if pd.isna(f) else f
    except (ValueError, TypeError):
        return None


# -- Bulk instrument resolve helpers ------------------------------------------
# Equity/derivatives/index below delegate to arthdesk_instruments.dao — same
# function names/signatures as before so nse_eq.py/bse_eq.py/nse_fo.py/
# bse_fo.py barely change; all old-shape-to-new-shape translation happens
# here. mf/mcx/prices helpers further down are untouched/deferred.

def bulk_resolve_equity(isins: list[str]) -> dict[str, int]:
    """isin -> instrument_id for all ISINs in the list."""
    if not isins:
        return {}
    with engine.connect() as conn:
        return dao.equity.bulk_resolve(conn, isins)


def bulk_create_equity(missing: list[dict]) -> dict[str, int]:
    """
    Bulk-create instruments for ISINs not yet in the master.
    Each item in missing: {isin, name?, nse_symbol?, nse_id?, bse_code?,
    bse_id?, face_value?} — face_value is REAL rupees (both nse_eq.py and
    bse_eq.py compute this directly via to_float now, no more paise
    round-trip). 'nse_symbol'/'bse_code' are the legacy key names; 'nse_id'/
    'bse_id'/'bse_sym' are accepted directly too. Returns {isin: instrument_id}.
    """
    if not missing:
        return {}
    rows = []
    for item in missing:
        rows.append({
            "isin": item["isin"],
            "nse_name": item.get("name"),
            "nse_sym": item.get("nse_symbol"),
            "nse_id": item.get("nse_id"),
            "bse_sym": item.get("bse_sym", item.get("bse_code")),
            "bse_id": item.get("bse_id"),
            "face_value": item.get("face_value"),
        })
    with engine.begin() as conn:
        result = dao.equity.bulk_create(conn, rows)
    for isin, instrument_id in result.items():
        logger.info("[base] Created EQUITY %d: %s", instrument_id, isin)
    return result


def bulk_update_equity_fields(updates: list[dict]):
    """
    Update metadata for existing equity instruments from bhavcopy.
    Each item: {instrument_id, name?, nse_symbol?, nse_id?, bse_code?,
    bse_id?, face_value?} — face_value is REAL rupees. 'name' has no home on
    instrument_equity anymore (nse_name/bse_name instead) — silently dropped
    if passed, since no current caller actually provides it.
    """
    if not updates:
        return
    translated = []
    for item in updates:
        fields = {"instr_id": item["instrument_id"]}
        if item.get("nse_symbol") is not None:
            fields["nse_sym"] = item["nse_symbol"]
        if item.get("nse_id") is not None:
            fields["nse_id"] = item["nse_id"]
        if item.get("bse_code") is not None:
            fields["bse_sym"] = item["bse_code"]
        if item.get("bse_id") is not None:
            fields["bse_id"] = item["bse_id"]
        if item.get("face_value") is not None:
            fields["face_value"] = item["face_value"]
        translated.append(fields)
    with engine.begin() as conn:
        dao.equity.update_fields(conn, translated)


def bulk_resolve_fo_nse(fin_ids: list[int]) -> dict[int, int]:
    """nse_bse_id -> instrument_id, scoped to NSE."""
    if not fin_ids:
        return {}
    with engine.connect() as conn:
        return dao.derivatives.bulk_resolve_by_id(conn, "NSE", fin_ids)


def bulk_resolve_fo_bse(fin_ids: list[int]) -> dict[int, int]:
    """nse_bse_id -> instrument_id, scoped to BSE."""
    if not fin_ids:
        return {}
    with engine.connect() as conn:
        return dao.derivatives.bulk_resolve_by_id(conn, "BSE", fin_ids)


def bulk_resolve_underlying_symbols(symbols: list[str]) -> dict[str, int]:
    """symbol -> underlying instrument_id, looping dao.derivatives.resolve_underlying
    per unique symbol (no bulk primitive for this — deliberate, see Phase 3/4
    plan: a bounded number of unique underlyings per file, each a cheap local
    lookup, not worth a dedicated batch primitive)."""
    if not symbols:
        return {}
    result: dict[str, int] = {}
    with engine.connect() as conn:
        for sym in symbols:
            instr_id = dao.derivatives.resolve_underlying(conn, sym, "NSE")
            if instr_id is not None:
                result[sym] = instr_id
    return result


# bulk_resolve_fo_contracts_by_underlying and get_fo_instrument_by_contract
# (cross-exchange contract dedup-by-composite-key) are deliberately removed,
# not migrated — that model only made sense under the old schema, where one
# row could represent both an NSE and a BSE contract via two separate id
# columns. The new schema gives each exchange's contract its own row (via
# the `exchange` discriminator), so there's no cross-exchange row to find.


def bulk_resolve_mcx(keys: list[tuple]) -> dict[tuple, int]:
    """
    ONE SELECT for all (mcx_symbol, instrument_type, expiry_date, strike_price_paise, option_type) tuples.
    Returns {key_tuple: instrument_id}.  strike_price_paise is an integer (paise).
    """
    if not keys:
        return {}
    result = {}
    syms = list({k[0] for k in keys})
    ph   = ",".join(f":s{n}" for n in range(len(syms)))
    with engine.connect() as conn:
        rows = conn.execute(
            text(f"""SELECT mcx_symbol, instrument_type, expiry_date, strike_price_paise, option_type, instrument_id
                     FROM instrument_mcx WHERE mcx_symbol IN ({ph})"""),
            {f"s{n}": v for n, v in enumerate(syms)},
        ).fetchall()
    for r in rows:
        key = (r[0].strip(), r[1], r[2], int(r[3]), r[4])
        result[key] = r[5]
    return result


def bulk_resolve_amfi(codes: list[str]) -> dict[str, int]:
    """ONE SELECT: amfi_code -> instrument_id."""
    if not codes:
        return {}
    ph = ",".join(f":c{n}" for n in range(len(codes)))
    with engine.connect() as conn:
        rows = conn.execute(
            text(f"SELECT amfi_code, instrument_id FROM instrument_mf WHERE amfi_code IN ({ph})"),
            {f"c{n}": v for n, v in enumerate(codes)},
        ).fetchall()
    return {r[0]: r[1] for r in rows}


# -- Single-item fallback helpers (for on-miss creates) -----------------------
# get_fo_instrument_by_contract removed along with its cross-exchange-dedup
# callers above — see note there.

def get_underlying_instrument_id(symbol: str) -> Optional[int]:
    """Behavior change, intentional: checks instrument_index before
    instrument_equity now (today's old code checked equity first) — this is
    the NIFTY-vs-NIFTYBEES fix (Phase 3's dao.derivatives.resolve_underlying),
    finally taking effect for BSE's sync path."""
    with engine.connect() as conn:
        return dao.derivatives.resolve_underlying(conn, symbol, "BSE")


def get_or_create_index(symbol: str, exchange: str = "NSE") -> int:
    """`exchange` accepted for call-site compatibility but ignored —
    instrument_index has no exchange column (Phase 2: small, curated,
    hand-seeded table where symbols don't collide across exchanges).
    Uses engine.begin() (not connect()) since this can write a new row."""
    with engine.begin() as conn:
        return dao.index.get_or_create(conn, symbol)


def get_or_create_mf(amfi_code: str, name: str, fund_house: str = None,
                     scheme_type: str = None) -> int:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT instrument_id FROM instrument_mf WHERE amfi_code=:code"),
            {"amfi_code": amfi_code}
        ).first()
        if row:
            return row[0]
    type_id = _get_type_id("EQUITY_MF")
    with engine.begin() as conn:
        r = conn.execute(text("""
            INSERT INTO instruments (name, instrument_type_id, is_active, created_at, updated_at)
            VALUES (:name, :type_id, 1, datetime('now'), datetime('now'))
        """), {"name": name, "type_id": type_id})
        instrument_id = r.lastrowid
        conn.execute(text("""
            INSERT OR IGNORE INTO instrument_mf (instrument_id, amfi_code, fund_house, scheme_type)
            VALUES (:iid, :code, :fh, :st)
        """), {"iid": instrument_id, "code": amfi_code, "fh": fund_house, "st": scheme_type})
    logger.info("[base] Auto-created MF: %s (%s) -> id=%d", name, amfi_code, instrument_id)
    return instrument_id


# -- Batch latest_prices upsert -----------------------------------------------

def batch_upsert_latest_prices(rows: list[dict]):
    """
    Batch upsert into latest_prices.
    Each row: {instrument_id, exchange, price_date, open_price_paise,
               high_price_paise, low_price_paise, close_price_paise}
    """
    if not rows:
        return
    # Keep only the most recent price per instrument_id (rows are date-ordered)
    deduped: dict[int, dict] = {}
    for r in rows:
        deduped[r["instrument_id"]] = r
    batch = list(deduped.values())
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO latest_prices
                (instrument_id, exchange, price_date,
                 open_price_paise, high_price_paise,
                 low_price_paise, close_price_paise,
                 last_synced_at, updated_at)
            VALUES
                (:instrument_id, :exchange, :price_date,
                 :open_price_paise, :high_price_paise,
                 :low_price_paise, :close_price_paise,
                 datetime('now'), datetime('now'))
            ON CONFLICT(instrument_id) DO UPDATE SET
                exchange          = excluded.exchange,
                price_date        = excluded.price_date,
                open_price_paise  = excluded.open_price_paise,
                high_price_paise  = excluded.high_price_paise,
                low_price_paise   = excluded.low_price_paise,
                close_price_paise = excluded.close_price_paise,
                last_synced_at    = datetime('now'),
                updated_at        = datetime('now')
        """), batch)


# -- Bulk create helpers -------------------------------------------------------

def bulk_create_fo(missing: list[dict], exchange: str) -> dict[int, int]:
    """
    Bulk-create FO instruments.
    Each item in missing: {fin_id, name, inst_kind, underlying_id, symbol,
                           instr_type, expiry_date, strike_paise, option_type, lot_size}
    `exchange`: 'NSE' or 'BSE' — callers used to pass the literal old column
    name ('nse_fininstrmid'/'bse_fininstrmid') here; now it's just the
    exchange, which is what the concept actually is under the new schema
    (exchange + nse_bse_id, not two separate id columns).
    Returns {fin_id: instrument_id}. This is also where the old
    instrument_type-vs-contract_type column bug is fixed by construction —
    dao.derivatives.bulk_create only ever writes real Core-defined columns.
    """
    if not missing:
        return {}
    rows = [{
        "nse_bse_id": item["fin_id"],
        "nse_bse_name": item["name"],
        "ul_instr_id": item["underlying_id"],
        "contract_type": item["instr_type"],
        "expd": item["expiry_date"],
        "strkp": item["strike_paise"] / 100,
        "opn_type": item["option_type"],
        "lot_size": item.get("lot_size"),
        "instrument_type_name": item["inst_kind"],
    } for item in missing]
    with engine.begin() as conn:
        result = dao.derivatives.bulk_create(conn, exchange, rows)
    for fin_id, instrument_id in result.items():
        logger.debug("[base] Created FO instrument %d (fin_id=%s)", instrument_id, fin_id)
    return result


def bulk_create_mcx(missing: list[dict]) -> dict[tuple, int]:
    """
    Bulk-create MCX instruments in ONE transaction.
    Each item: {key, name, inst_kind, mcx_symbol, instr_type, expiry_date,
                strike_paise, option_type, unit}
    Returns {key_tuple: instrument_id}.
    """
    if not missing:
        return {}
    result = {}
    with engine.begin() as conn:
        for item in missing:
            type_id = _get_type_id(item["inst_kind"])
            r = conn.execute(text("""
                INSERT INTO instruments (name, instrument_type_id, is_active, created_at, updated_at)
                VALUES (:name, :type_id, 1, datetime('now'), datetime('now'))
            """), {"name": item["name"], "type_id": type_id})
            instrument_id = r.lastrowid

            conn.execute(text("""
                INSERT OR IGNORE INTO instrument_mcx (
                    instrument_id, mcx_symbol, instrument_type, expiry_date,
                    strike_price_paise, option_type, unit
                ) VALUES (:iid, :sym, :itype, :exp, :strike, :opt, :unit)
            """), {
                "iid":   instrument_id,
                "sym":   item["mcx_symbol"],
                "itype": item["instr_type"],
                "exp":   item["expiry_date"],
                "strike": item["strike_paise"],
                "opt":   item["option_type"],
                "unit":  item.get("unit"),
            })

            result[item["key"]] = instrument_id
            logger.debug("[base] Created MCX instrument %d: %s", instrument_id, item["name"])
    return result


def bulk_create_mf(missing: list[dict]) -> dict[str, int]:
    """
    Bulk-create MF instruments in ONE transaction.
    Each item: {amfi_code, name, fund_house, scheme_type}
    Returns {amfi_code: instrument_id}.
    """
    if not missing:
        return {}
    result = {}
    type_id = _get_type_id("EQUITY_MF")
    with engine.begin() as conn:
        for item in missing:
            r = conn.execute(text("""
                INSERT INTO instruments (name, instrument_type_id, is_active, created_at, updated_at)
                VALUES (:name, :type_id, 1, datetime('now'), datetime('now'))
            """), {"name": item["name"], "type_id": type_id})
            instrument_id = r.lastrowid

            conn.execute(text("""
                INSERT OR IGNORE INTO instrument_mf
                    (instrument_id, amfi_code, fund_house, scheme_type)
                VALUES (:iid, :code, :fh, :st)
            """), {
                "iid":  instrument_id,
                "code": item["amfi_code"],
                "fh":   item.get("fund_house"),
                "st":   item.get("scheme_type"),
            })

            result[item["amfi_code"]] = instrument_id
            logger.debug("[base] Created MF instrument %d: %s (%s)", instrument_id, item["name"], item["amfi_code"])
    return result
