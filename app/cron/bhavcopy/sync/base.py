"""
Shared utilities for all bhavcopy sync parsers.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import pandas as pd
from sqlalchemy import text

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

def bulk_resolve_equity(isins: list[str]) -> dict[str, int]:
    """ONE SELECT: isin -> instrument_id for all ISINs in the list."""
    if not isins:
        return {}
    ph = ",".join(f":i{n}" for n in range(len(isins)))
    with engine.connect() as conn:
        rows = conn.execute(
            text(f"SELECT isin, instrument_id FROM instrument_equity WHERE isin IN ({ph})"),
            {f"i{n}": v for n, v in enumerate(isins)},
        ).fetchall()
    return {r[0]: r[1] for r in rows}


def bulk_create_equity(missing: list[dict]) -> dict[str, int]:
    """
    Bulk-create instruments for ISINs not yet in the master.
    Each item in missing: {isin, name, nse_symbol, bse_code, face_value_paise}
    Returns {isin: instrument_id}.
    """
    if not missing:
        return {}
    result = {}
    type_id = _get_type_id("EQUITY")
    with engine.begin() as conn:
        for item in missing:
            r = conn.execute(text("""
                INSERT INTO instruments (name, instrument_type_id, is_active, created_at, updated_at)
                VALUES (:name, :type_id, 1, datetime('now'), datetime('now'))
            """), {"name": item["name"], "type_id": type_id})
            instrument_id = r.lastrowid
            conn.execute(text("""
                INSERT INTO instrument_equity
                    (instrument_id, isin, nse_symbol, bse_code, face_value_paise)
                VALUES (:iid, :isin, :nse_symbol, :bse_code, :fv)
                ON CONFLICT(instrument_id) DO UPDATE SET
                    nse_symbol       = COALESCE(excluded.nse_symbol, nse_symbol),
                    bse_code         = COALESCE(excluded.bse_code, bse_code),
                    face_value_paise = COALESCE(excluded.face_value_paise, face_value_paise)
            """), {
                "iid":        instrument_id,
                "isin":       item["isin"],
                "nse_symbol": item.get("nse_symbol"),
                "bse_code":   item.get("bse_code"),
                "fv":         item.get("face_value_paise"),
            })
            result[item["isin"]] = instrument_id
            logger.info("[base] Created EQUITY %d: %s (%s)", instrument_id, item["name"], item["isin"])
    return result


def bulk_update_equity_fields(updates: list[dict]):
    """
    Update metadata for existing equity instruments from bhavcopy.
    Each item: {instrument_id, name?, nse_symbol?, bse_code?, face_value_paise?}

    Updates instruments.name conditionally (WHERE name != new_name) to avoid
    spurious updated_at trigger bumps when nothing changed.
    Extension fields are overwritten if provided (not COALESCE) so bhavcopy
    corrections propagate.
    """
    if not updates:
        return
    with engine.begin() as conn:
        for item in updates:
            iid = item["instrument_id"]

            # Update instruments.name only if it actually changed
            if item.get("name") is not None:
                conn.execute(text("""
                    UPDATE instruments SET name = :name, updated_at = datetime('now')
                    WHERE instrument_id = :iid AND name != :name
                """), {"name": item["name"], "iid": iid})

            # Update extension fields that are explicitly provided
            ext_fields = {k: v for k, v in item.items()
                          if k not in ("instrument_id", "name") and v is not None}
            if not ext_fields:
                continue
            set_clause = ", ".join(f"{k} = :{k}" for k in ext_fields)
            conn.execute(
                text(f"UPDATE instrument_equity SET {set_clause} WHERE instrument_id = :iid"),
                {**ext_fields, "iid": iid}
            )


def bulk_resolve_fo_nse(fin_ids: list[int]) -> dict[int, int]:
    """ONE SELECT: nse_fininstrmid -> instrument_id."""
    if not fin_ids:
        return {}
    ph = ",".join(f":i{n}" for n in range(len(fin_ids)))
    with engine.connect() as conn:
        rows = conn.execute(
            text(f"SELECT nse_fininstrmid, instrument_id FROM instrument_derivatives WHERE nse_fininstrmid IN ({ph})"),
            {f"i{n}": v for n, v in enumerate(fin_ids)},
        ).fetchall()
    return {r[0]: r[1] for r in rows}


def bulk_resolve_fo_bse(fin_ids: list[int]) -> dict[int, int]:
    """ONE SELECT: bse_fininstrmid -> instrument_id."""
    if not fin_ids:
        return {}
    ph = ",".join(f":i{n}" for n in range(len(fin_ids)))
    with engine.connect() as conn:
        rows = conn.execute(
            text(f"SELECT bse_fininstrmid, instrument_id FROM instrument_derivatives WHERE bse_fininstrmid IN ({ph})"),
            {f"i{n}": v for n, v in enumerate(fin_ids)},
        ).fetchall()
    return {r[0]: r[1] for r in rows}


def bulk_resolve_underlying_symbols(symbols: list[str]) -> dict[str, int]:
    """2 SELECTs (equity + index): symbol -> underlying instrument_id."""
    if not symbols:
        return {}
    ph = ",".join(f":s{n}" for n in range(len(symbols)))
    params = {f"s{n}": s for n, s in enumerate(symbols)}
    result: dict[str, int] = {}
    with engine.connect() as conn:
        for table, col in [("instrument_equity", "nse_symbol"), ("instrument_index", "symbol")]:
            for r in conn.execute(
                text(f"SELECT {col}, instrument_id FROM {table} WHERE {col} IN ({ph})"),
                params,
            ).fetchall():
                if r[0] not in result:
                    result[r[0]] = r[1]
    return result


def bulk_resolve_fo_contracts_by_underlying(underlying_ids: list[int]) -> dict[tuple, int]:
    """ONE SELECT: (underlying_id, expiry_date, strike_price_paise, option_type) -> instrument_id."""
    if not underlying_ids:
        return {}
    ph = ",".join(f":u{n}" for n in range(len(underlying_ids)))
    with engine.connect() as conn:
        rows = conn.execute(
            text(f"""
                SELECT instrument_id, underlying_instrument_id, expiry_date,
                       strike_price_paise, option_type
                FROM instrument_derivatives
                WHERE underlying_instrument_id IN ({ph})
            """),
            {f"u{n}": u for n, u in enumerate(underlying_ids)},
        ).fetchall()
    return {(r[1], r[2], int(r[3]), r[4]): r[0] for r in rows}


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

def get_fo_instrument_by_contract(
    underlying_instrument_id: int,
    expiry_date: str,
    strike_price_paise: int,
    option_type: str,
) -> Optional[int]:
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT instrument_id FROM instrument_derivatives
            WHERE underlying_instrument_id=:uid
              AND expiry_date=:exp
              AND strike_price_paise=:strike
              AND option_type=:opt
        """), {"uid": underlying_instrument_id, "exp": expiry_date,
               "strike": strike_price_paise, "opt": option_type}).first()
    return row[0] if row else None


def get_underlying_instrument_id(symbol: str) -> Optional[int]:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT instrument_id FROM instrument_equity WHERE nse_symbol=:sym"),
            {"sym": symbol}
        ).first()
        if row:
            return row[0]
        row = conn.execute(
            text("SELECT instrument_id FROM instrument_index WHERE symbol=:sym"),
            {"sym": symbol}
        ).first()
        if row:
            return row[0]
    return None


def get_or_create_index(symbol: str, exchange: str = "NSE") -> int:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT instrument_id FROM instrument_index WHERE symbol=:sym AND exchange=:exc"),
            {"sym": symbol, "exc": exchange}
        ).first()
        if row:
            return row[0]
    type_id = _get_type_id("INDEX")
    with engine.begin() as conn:
        r = conn.execute(text("""
            INSERT INTO instruments (name, instrument_type_id, is_active, created_at, updated_at)
            VALUES (:name, :type_id, 1, datetime('now'), datetime('now'))
        """), {"name": symbol, "type_id": type_id})
        instrument_id = r.lastrowid
        conn.execute(text("""
            INSERT OR IGNORE INTO instrument_index (instrument_id, symbol, exchange)
            VALUES (:iid, :sym, :exc)
        """), {"iid": instrument_id, "sym": symbol, "exc": exchange})
    logger.info("[base] Auto-created INDEX: %s -> id=%d", symbol, instrument_id)
    return instrument_id


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

def bulk_create_fo(missing: list[dict], exchange_col: str) -> dict[int, int]:
    """
    Bulk-create FO instruments in ONE transaction.
    Each item in missing: {fin_id, name, inst_kind, underlying_id, symbol,
                           instr_type, expiry_date, strike_paise, option_type, lot_size}
    exchange_col: 'nse_fininstrmid' or 'bse_fininstrmid'
    Returns {fin_id: instrument_id}.
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

            conn.execute(text(f"""
                INSERT OR IGNORE INTO instrument_derivatives (
                    instrument_id, underlying_instrument_id, underlying_symbol,
                    instrument_type, expiry_date, strike_price_paise, option_type,
                    lot_size, {exchange_col}
                ) VALUES (
                    :iid, :uid, :sym, :itype, :exp, :strike, :opt, :lot, :fin_id
                )
            """), {
                "iid":    instrument_id,
                "uid":    item["underlying_id"],
                "sym":    item["symbol"],
                "itype":  item["instr_type"],
                "exp":    item["expiry_date"],
                "strike": item["strike_paise"],
                "opt":    item["option_type"],
                "lot":    item.get("lot_size"),
                "fin_id": item["fin_id"],
            })

            result[item["fin_id"]] = instrument_id
            logger.debug("[base] Created FO instrument %d: %s", instrument_id, item["name"])
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
