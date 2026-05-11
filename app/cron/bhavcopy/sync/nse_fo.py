"""
NSE F&O bhavcopy parser.

Reads downloaded BhavCopy_NSE_FO_*_F_0000.csv files.

Lookup:  FinInstrmId -> instrument_fo.nse_fininstrmid -> instrument_id
On miss: bulk-create all missing contracts in ONE transaction.
Writes:  fo_eod (exchange='NSE')
"""
from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd
from sqlalchemy import text

from app.database import engine
from app.cron.bhavcopy.sync.base import (
    get_pending_files, resolve_file_path, mark_synced, mark_failed,
    to_paise, to_int, to_float,
    bulk_resolve_fo_nse, bulk_create_fo,
    get_fo_instrument_by_contract,
    get_underlying_instrument_id, get_or_create_index,
)

logger = logging.getLogger(__name__)
SOURCE   = "NSE_FO"
EXCHANGE = "NSE"

_TYPE_MAP = {
    "IDF": "FUTURES",
    "STF": "FUTURES",
    "IDO": "OPTIONS",
    "STO": "OPTIONS",
}


def run(force: bool = False) -> dict:
    files = get_pending_files(SOURCE)
    if not files:
        return _stats(0, 0, 0, [])

    total_rows   = 0
    files_synced = 0
    files_failed = 0
    errors       = []

    for f in files:
        try:
            rows = _process_file(f["file_name"], f["trade_date"])
            mark_synced(f["file_name"], rows)
            total_rows += rows
            files_synced += 1
            logger.info("[%s] Synced %s -- %d rows", SOURCE, f["file_name"], rows)
        except Exception as exc:
            mark_failed(f["file_name"], str(exc))
            files_failed += 1
            errors.append({"file": f["file_name"], "error": str(exc)})
            logger.error("[%s] Failed %s: %s", SOURCE, f["file_name"], exc, exc_info=True)

    return _stats(files_synced, files_failed, total_rows, errors)


def _process_file(file_name: str, trade_date_str: str) -> int:
    path = resolve_file_path(trade_date_str, file_name)
    df   = pd.read_csv(path, dtype=str)
    df.columns = df.columns.str.strip()

    # -- Pass 1: parse all valid rows, collect unique fin_ids ------------------
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

    # -- Pass 2: ONE SELECT for all unique fin_ids -----------------------------
    unique_fin_ids = list({fid for fid, _ in parsed_rows})
    id_map         = bulk_resolve_fo_nse(unique_fin_ids)

    # -- Pass 3: resolve missing contracts, bulk-create in ONE transaction -----
    missing_ids = [fid for fid in unique_fin_ids if fid not in id_map]
    if missing_ids:
        # Separate into: contracts that link to an existing record vs truly new
        linked, new_specs = _resolve_missing(missing_ids, parsed_rows)
        # Inject already-linked ids directly
        id_map.update(linked)
        # Bulk-create all truly new contracts in a single transaction
        if new_specs:
            new_ids = bulk_create_fo(new_specs, "nse_fininstrmid")
            id_map.update(new_ids)

    # -- Pass 4: build fo_eod batch --------------------------------------------
    batch = []
    for fin_id, row in parsed_rows:
        inst_id = id_map.get(fin_id)
        if inst_id is None:
            skipped += 1
            continue

        trade_date = str(row.get("TradDt", trade_date_str)).strip()[:10]
        batch.append({
            "instrument_id":          inst_id,
            "exchange":               EXCHANGE,
            "trade_date":             trade_date,
            "open_price_paise":       to_paise(row.get("OpnPric")),
            "high_price_paise":       to_paise(row.get("HghPric")),
            "low_price_paise":        to_paise(row.get("LwPric")),
            "close_price_paise":      to_paise(row.get("ClsPric")),
            "last_price_paise":       to_paise(row.get("LastPric")),
            "prev_close_paise":       to_paise(row.get("PrvsClsgPric")),
            "underlying_price_paise": to_paise(row.get("UndrlygPric")),
            "settlement_price_paise": to_paise(row.get("SttlmPric")),
            "open_interest":          to_int(row.get("OpnIntrst")),
            "oi_change":              to_int(row.get("ChngInOpnIntrst")),
            "volume":                 to_int(row.get("TtlTradgVol")),
            "traded_value_rupees":    to_float(row.get("TtlTrfVal")),
            "num_trades":             to_int(row.get("TtlNbOfTxsExctd")),
        })

    if batch:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO fo_eod (
                    instrument_id, exchange, trade_date,
                    open_price_paise, high_price_paise, low_price_paise,
                    close_price_paise, last_price_paise, prev_close_paise,
                    underlying_price_paise, settlement_price_paise,
                    open_interest, oi_change, volume, traded_value_rupees, num_trades
                ) VALUES (
                    :instrument_id, :exchange, :trade_date,
                    :open_price_paise, :high_price_paise, :low_price_paise,
                    :close_price_paise, :last_price_paise, :prev_close_paise,
                    :underlying_price_paise, :settlement_price_paise,
                    :open_interest, :oi_change, :volume, :traded_value_rupees, :num_trades
                )
                ON CONFLICT(instrument_id, exchange, trade_date) DO UPDATE SET
                    open_price_paise=excluded.open_price_paise,
                    high_price_paise=excluded.high_price_paise,
                    low_price_paise=excluded.low_price_paise,
                    close_price_paise=excluded.close_price_paise,
                    last_price_paise=excluded.last_price_paise,
                    prev_close_paise=excluded.prev_close_paise,
                    underlying_price_paise=excluded.underlying_price_paise,
                    settlement_price_paise=excluded.settlement_price_paise,
                    open_interest=excluded.open_interest,
                    oi_change=excluded.oi_change,
                    volume=excluded.volume,
                    traded_value_rupees=excluded.traded_value_rupees,
                    num_trades=excluded.num_trades
            """), batch)

    if skipped:
        logger.debug("[%s] %s -- skipped %d rows", SOURCE, file_name, skipped)

    return len(batch)


def _resolve_missing(
    missing_ids: list[int],
    parsed_rows: list,
) -> tuple[dict[int, int], list[dict]]:
    """
    For each missing fin_id:
      - If the same contract already exists under a different fin_id, link it
        and return {fin_id: existing_instrument_id} in `linked`.
      - Otherwise build a spec dict for bulk_create_fo and add to `new_specs`.
    Returns (linked_map, new_specs).
    """
    fid_to_row: dict[int, object] = {}
    for fid, row in parsed_rows:
        if fid in missing_ids and fid not in fid_to_row:
            fid_to_row[fid] = row

    linked:    dict[int, int] = {}
    new_specs: list[dict]     = []

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

        underlying_id = get_underlying_instrument_id(symbol)
        if underlying_id is None:
            underlying_id = get_or_create_index(symbol, EXCHANGE)

        # Same contract may already exist created by a previous file or BSE FO
        existing_id = get_fo_instrument_by_contract(
            underlying_id, expiry_date, strike_paise, option_type
        )
        if existing_id:
            with engine.begin() as conn:
                conn.execute(text("""
                    UPDATE instrument_derivatives SET nse_fininstrmid=:fid WHERE instrument_id=:iid
                """), {"fid": fid, "iid": existing_id})
            linked[fid] = existing_id
            continue

        inst_kind = _TYPE_MAP.get(instr_type, "FUTURES")
        name = (f"{symbol} {expiry_date} {strike_raw} {option_type}"
                if option_type in ("CE", "PE")
                else f"{symbol} {expiry_date} FUT")

        new_specs.append({
            "fin_id":        fid,
            "name":          name,
            "inst_kind":     inst_kind,
            "underlying_id": underlying_id,
            "symbol":        symbol,
            "instr_type":    instr_type,
            "expiry_date":   expiry_date,
            "strike_paise":  strike_paise,
            "option_type":   option_type,
            "lot_size":      lot_size,
        })

    return linked, new_specs


def _stats(synced, failed, rows, errors):
    return {
        "source":            SOURCE,
        "files_synced":      synced,
        "files_failed":      failed,
        "total_rows_synced": rows,
        "errors":            errors,
    }
