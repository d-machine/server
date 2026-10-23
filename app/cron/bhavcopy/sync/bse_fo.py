"""
BSE F&O bhavcopy parser.

Lookup:  FinInstrmId -> instrument_derivatives.nse_bse_id (exchange='BSE') -> instr_id
On miss: bulk-create all missing contracts.
Writes:  fo_eod (exchange='BSE')

No cross-exchange contract matching (removed) — NSE and BSE F&O contracts
are separate rows by design (exchange discriminator), so a BSE sync never
links to a row NSE's sync created, and vice versa.

All DB access for one file goes through a single arthdesk_db.Database,
constructed via app.database.db_session() — one connection/transaction per
file, committed once, instead of a separate one per step.
"""
from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd
from sqlalchemy import text

from app.database import db_session
from app.cron.bhavcopy.sync.base import (
    get_pending_files, load_file_df, mark_synced, mark_failed,
    to_int, to_float,
    bulk_resolve_fo_bse_full, bulk_create_fo,
    get_underlying_instrument_id, get_or_create_index,
    upsert_latest_prices,
    instrument_write_lock,
)

logger = logging.getLogger(__name__)
SOURCE   = "BSE_FO"
EXCHANGE = "BSE"

_TYPE_MAP = {
    "IDF": "FUTURES",
    "STF": "FUTURES",
    "IDO": "OPTIONS",
    "STO": "OPTIONS",
}


def run(force: bool = False) -> dict:
    with db_session() as db:
        files = get_pending_files(db, SOURCE, include_failed=force)
    if not files:
        return _stats(0, 0, 0, [])

    total_rows   = 0
    files_synced = 0
    files_failed = 0
    errors       = []

    for f in files:
        try:
            with instrument_write_lock, db_session() as db:
                rows = _process_file(db, f["file_name"], f["trade_date"])
                mark_synced(db, f["file_name"], rows)
                db.commit()
            total_rows += rows
            files_synced += 1
            logger.info("[%s] Synced %s -- %d rows", SOURCE, f["file_name"], rows)
        except Exception as exc:
            with db_session() as db:
                mark_failed(db, f["file_name"], str(exc))
                db.commit()
            files_failed += 1
            errors.append({"file": f["file_name"], "error": str(exc)})
            logger.error("[%s] Failed %s: %s", SOURCE, f["file_name"], exc, exc_info=True)

    return _stats(files_synced, files_failed, total_rows, errors)


def _process_file(db, file_name: str, trade_date_str: str) -> int:
    df = load_file_df(trade_date_str, file_name, dtype=str)
    df.columns = df.columns.str.strip()

    parsed_rows = []
    skipped     = 0

    for _, row in df.iterrows():
        fin_id_raw = row.get("FinInstrmId", "")
        try:
            fin_id = int(float(str(fin_id_raw).strip()))
        except (ValueError, TypeError):
            skipped += 1
            continue
        parsed_rows.append((fin_id, row))

    if not parsed_rows:
        return 0

    unique_fin_ids = list({fid for fid, _ in parsed_rows})

    # Metadata for every fin_id up front -- needed both to validate an
    # id-based hit and to build create-specs for a genuine miss.
    fid_meta = _build_fid_meta(db, unique_fin_ids, parsed_rows)

    # Validate each id-based hit against fid_meta rather than trusting the
    # id alone -- the exchange can recycle an id for an unrelated contract
    # once the original expires (see nse_fo.py's _row_matches, same
    # reasoning applies to BSE). A mismatch falls through to the
    # find-or-create path below, same as a genuine miss.
    raw_hits = bulk_resolve_fo_bse_full(db, unique_fin_ids)
    id_map: dict[int, int] = {}
    missing_ids: list[int] = []
    for fid in unique_fin_ids:
        meta = fid_meta.get(fid)
        if meta is None:
            continue
        stored = raw_hits.get(fid)
        if stored is not None and _row_matches(stored, meta):
            id_map[fid] = stored["instr_id"]
        else:
            missing_ids.append(fid)

    if missing_ids:
        new_specs = _specs_from_meta(fid_meta, missing_ids)
        if new_specs:
            new_ids = bulk_create_fo(db, new_specs, "BSE")
            id_map.update(new_ids)

    batch = []
    for fin_id, row in parsed_rows:
        inst_id = id_map.get(fin_id)
        if inst_id is None:
            skipped += 1
            continue

        trade_date = str(row.get("TradDt", trade_date_str)).strip()[:10]
        batch.append({
            "instr_id":          inst_id,
            "exchange":          EXCHANGE,
            "trade_date":        trade_date,
            "open_price":        to_float(row.get("OpnPric")),
            "high_price":        to_float(row.get("HghPric")),
            "low_price":         to_float(row.get("LwPric")),
            "close_price":       to_float(row.get("ClsPric")),
            "last_price":        to_float(row.get("LastPric")),
            "prev_close_price":  to_float(row.get("PrvsClsgPric")),
            "underlying_price":  to_float(row.get("UndrlygPric")),
            "settlement_price":  to_float(row.get("SttlmPric")),
            "open_interest":     to_int(row.get("OpnIntrst")),
            "oi_change":         to_int(row.get("ChngInOpnIntrst")),
            "volume":            to_int(row.get("TtlTradgVol")),
            "traded_value_rupees": to_float(row.get("TtlTrfVal")),
            "num_trades":        to_int(row.get("TtlNbOfTxsExctd")),
        })

    if batch:
        latest_batch = [
            {"instr_id": row["instr_id"], "exchange": row["exchange"],
             "price_date": row["trade_date"], "price": row["close_price"]}
            for row in batch if row["close_price"] is not None
        ]
        upsert_latest_prices(db, latest_batch)

        db.execute(text("""
            INSERT INTO fo_eod (
                instr_id, exchange, trade_date,
                open_price, high_price, low_price,
                close_price, last_price, prev_close_price,
                underlying_price, settlement_price,
                open_interest, oi_change, volume, traded_value_rupees, num_trades
            ) VALUES (
                :instr_id, :exchange, :trade_date,
                :open_price, :high_price, :low_price,
                :close_price, :last_price, :prev_close_price,
                :underlying_price, :settlement_price,
                :open_interest, :oi_change, :volume, :traded_value_rupees, :num_trades
            )
            ON CONFLICT(instr_id, exchange, trade_date) DO UPDATE SET
                open_price=excluded.open_price,
                high_price=excluded.high_price,
                low_price=excluded.low_price,
                close_price=excluded.close_price,
                last_price=excluded.last_price,
                prev_close_price=excluded.prev_close_price,
                underlying_price=excluded.underlying_price,
                settlement_price=excluded.settlement_price,
                open_interest=excluded.open_interest,
                oi_change=excluded.oi_change,
                volume=excluded.volume,
                traded_value_rupees=excluded.traded_value_rupees,
                num_trades=excluded.num_trades
        """), batch)

    if skipped:
        logger.debug("[%s] %s -- skipped %d rows", SOURCE, file_name, skipped)

    return len(batch)


def _build_fid_meta(db, fin_ids: list[int], parsed_rows: list) -> dict[int, dict]:
    """
    Parse + resolve full contract metadata for EVERY given fin_id, not just
    ones missing from the fast id-based lookup -- needed to validate an
    id-based hit (_row_matches) as well as to build the create-spec for a
    genuine miss. Mirrors nse_fo.py's _build_fid_meta; see its docstring
    for why this always runs rather than being skipped for known ids.
    """
    fid_set = set(fin_ids)
    fid_to_row: dict[int, object] = {}
    for fid, row in parsed_rows:
        if fid in fid_set and fid not in fid_to_row:
            fid_to_row[fid] = row

    fid_meta: dict[int, dict] = {}
    for fid, row in fid_to_row.items():
        symbol     = str(row.get("TckrSymb", "")).strip()
        instr_type = str(row.get("FinInstrmTp", "")).strip()
        expiry_raw = str(row.get("XpryDt", "")).strip()
        strike_raw = row.get("StrkPric", "0")
        option_raw = str(row.get("OptnTp", "")).strip()
        lot_size   = to_int(row.get("NewBrdLotQty"))

        try:
            expiry_date = datetime.strptime(expiry_raw, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            logger.warning("[%s] Bad expiry '%s' for FinInstrmId=%d -- skipped", SOURCE, expiry_raw, fid)
            continue

        try:
            strike_paise = int(round(float(strike_raw) * 100)) if strike_raw else 0
        except (ValueError, TypeError):
            strike_paise = 0
        option_type = option_raw if option_raw in ("CE", "PE") else "-"

        underlying_id = get_underlying_instrument_id(db, symbol)
        if underlying_id is None:
            underlying_id = get_or_create_index(db, symbol, "BSE")

        fid_meta[fid] = {
            "symbol":        symbol,
            "instr_type":    instr_type,
            "expiry_date":   expiry_date,
            "strike_raw":    strike_raw,
            "strike_paise":  strike_paise,
            "option_type":   option_type,
            "lot_size":      lot_size,
            "underlying_id": underlying_id,
        }

    return fid_meta


def _row_matches(stored: dict, meta: dict) -> bool:
    """See nse_fo.py's _row_matches -- identical reasoning, BSE can recycle
    FinInstrmId the same way NSE does."""
    stored_strike = stored.get("strkp") or 0.0
    meta_strike = meta["strike_paise"] / 100
    return (
        stored.get("ul_instr_id") == meta["underlying_id"]
        and stored.get("expd") == meta["expiry_date"]
        and abs(stored_strike - meta_strike) < 1e-6
        and (stored.get("opn_type") or "-") == meta["option_type"]
    )


def _specs_from_meta(fid_meta: dict, fids: list[int]) -> list[dict]:
    """Build bulk_create_fo's create-spec dicts from already-computed
    fid_meta (see _build_fid_meta) for the given fids -- no re-parsing."""
    specs: list[dict] = []
    for fid in fids:
        meta = fid_meta.get(fid)
        if meta is None:
            continue
        symbol = meta["symbol"]
        name = (f"{symbol} {meta['expiry_date']} {meta['strike_raw']} {meta['option_type']}"
                if meta["option_type"] in ("CE", "PE")
                else f"{symbol} {meta['expiry_date']} FUT")
        specs.append({
            "fin_id":        fid,
            "name":          name,
            "inst_kind":     _TYPE_MAP.get(meta["instr_type"], "FUTURES"),
            "underlying_id": meta["underlying_id"],
            "symbol":        symbol,
            "instr_type":    meta["instr_type"],
            "expiry_date":   meta["expiry_date"],
            "strike_paise":  meta["strike_paise"],
            "option_type":   meta["option_type"],
            "lot_size":      meta["lot_size"],
        })
    return specs


def _stats(synced, failed, rows, errors):
    return {
        "source":            SOURCE,
        "files_synced":      synced,
        "files_failed":      failed,
        "total_rows_synced": rows,
        "errors":            errors,
    }
