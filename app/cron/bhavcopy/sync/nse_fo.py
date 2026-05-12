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
    get_pending_files, load_file_chunks, mark_synced, mark_failed,
    to_paise, to_int, to_float,
    bulk_resolve_fo_nse, bulk_create_fo,
    bulk_resolve_underlying_symbols, bulk_resolve_fo_contracts_by_underlying,
    get_or_create_index,
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
    total = 0
    for i, chunk in enumerate(load_file_chunks(trade_date_str, file_name), 1):
        chunk.columns = chunk.columns.str.strip()
        rows = _process_chunk(chunk, trade_date_str, file_name)
        logger.debug("[%s] %s chunk %d — %d rows", SOURCE, file_name, i, rows)
        total += rows
    return total


def _process_chunk(df: pd.DataFrame, trade_date_str: str, file_name: str) -> int:
    # -- Pass 1: vectorized fin_id parsing ------------------------------------
    df = df.copy()
    df["fin_id_parsed"] = pd.to_numeric(
        df["FinInstrmId"].astype(str).str.strip(), errors="coerce"
    )
    skipped = int(df["fin_id_parsed"].isna().sum())
    df = df[df["fin_id_parsed"].notna()].copy()
    if df.empty:
        return 0
    df["fin_id_parsed"] = df["fin_id_parsed"].astype(int)

    # -- Pass 2: ONE SELECT for all unique fin_ids in this chunk --------------
    unique_fin_ids = df["fin_id_parsed"].unique().tolist()
    id_map = bulk_resolve_fo_nse(unique_fin_ids)

    # -- Pass 3: batch-resolve missing contracts, bulk-create -----------------
    missing_ids = [fid for fid in unique_fin_ids if fid not in id_map]
    if missing_ids:
        linked, new_specs = _resolve_missing(missing_ids, df)
        id_map.update(linked)
        if new_specs:
            id_map.update(bulk_create_fo(new_specs, "nse_fininstrmid"))

    # -- Pass 4: map instrument_ids vectorized --------------------------------
    df["inst_id_mapped"] = df["fin_id_parsed"].map(id_map)
    skipped += int(df["inst_id_mapped"].isna().sum())
    df = df[df["inst_id_mapped"].notna()].copy()
    if df.empty:
        if skipped:
            logger.debug("[%s] %s -- skipped %d rows", SOURCE, file_name, skipped)
        return 0
    df["inst_id_mapped"] = df["inst_id_mapped"].astype(int)

    # -- Pass 5: build fo_eod batch with itertuples ---------------------------
    batch = []
    for row in df.itertuples(index=False):
        trade_date = str(getattr(row, "TradDt", trade_date_str)).strip()[:10]
        batch.append({
            "instrument_id":          row.inst_id_mapped,
            "exchange":               EXCHANGE,
            "trade_date":             trade_date,
            "open_price_paise":       to_paise(getattr(row, "OpnPric", None)),
            "high_price_paise":       to_paise(getattr(row, "HghPric", None)),
            "low_price_paise":        to_paise(getattr(row, "LwPric", None)),
            "close_price_paise":      to_paise(getattr(row, "ClsPric", None)),
            "last_price_paise":       to_paise(getattr(row, "LastPric", None)),
            "prev_close_paise":       to_paise(getattr(row, "PrvsClsgPric", None)),
            "underlying_price_paise": to_paise(getattr(row, "UndrlygPric", None)),
            "settlement_price_paise": to_paise(getattr(row, "SttlmPric", None)),
            "open_interest":          to_int(getattr(row, "OpnIntrst", None)),
            "oi_change":              to_int(getattr(row, "ChngInOpnIntrst", None)),
            "volume":                 to_int(getattr(row, "TtlTradgVol", None)),
            "traded_value_rupees":    to_float(getattr(row, "TtlTrfVal", None)),
            "num_trades":             to_int(getattr(row, "TtlNbOfTxsExctd", None)),
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


def _resolve_missing(missing_ids: list[int], df: pd.DataFrame) -> tuple[dict[int, int], list[dict]]:
    """
    Batch-resolve all missing fin_ids to instrument_ids using 3 total SELECTs
    (2 for symbol lookup + 1 for contract lookup) instead of N queries per fin_id.
    """
    missing_set = set(missing_ids)

    # One representative row per missing fin_id
    fid_meta: dict[int, dict] = {}
    subset = df[df["fin_id_parsed"].isin(missing_set)].drop_duplicates("fin_id_parsed")
    for row in subset.itertuples(index=False):
        fid        = row.fin_id_parsed
        symbol     = str(getattr(row, "TckrSymb", "")).strip()
        instr_type = str(getattr(row, "FinInstrmTp", "")).strip()
        expiry_raw = str(getattr(row, "XpryDt", "")).strip()
        strike_raw = str(getattr(row, "StrkPric", "0") or "0")
        option_raw = str(getattr(row, "OptnTp", "")).strip()
        lot_size   = to_int(getattr(row, "NewBrdLotQty", None))

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

        fid_meta[fid] = {
            "symbol":       symbol,
            "instr_type":   instr_type,
            "expiry_date":  expiry_date,
            "strike_raw":   strike_raw,
            "strike_paise": strike_paise,
            "option_type":  option_type,
            "lot_size":     lot_size,
        }

    if not fid_meta:
        return {}, []

    # BATCH 1: resolve all unique symbols -> underlying instrument_id (2 SELECTs)
    unique_symbols = list({m["symbol"] for m in fid_meta.values()})
    symbol_map = bulk_resolve_underlying_symbols(unique_symbols)
    for sym in unique_symbols:
        if sym not in symbol_map:
            symbol_map[sym] = get_or_create_index(sym, EXCHANGE)

    # BATCH 2: resolve all existing contracts (1 SELECT)
    underlying_ids = list({symbol_map[m["symbol"]] for m in fid_meta.values()})
    contract_map = bulk_resolve_fo_contracts_by_underlying(underlying_ids)

    linked:      dict[int, int] = {}
    new_specs:   list[dict]     = []
    link_updates: list[dict]    = []

    for fid, meta in fid_meta.items():
        underlying_id = symbol_map[meta["symbol"]]
        contract_key  = (underlying_id, meta["expiry_date"], meta["strike_paise"], meta["option_type"])
        existing_id   = contract_map.get(contract_key)

        if existing_id:
            link_updates.append({"fid": fid, "iid": existing_id})
            linked[fid] = existing_id
            continue

        inst_kind = _TYPE_MAP.get(meta["instr_type"], "FUTURES")
        symbol    = meta["symbol"]
        name = (
            f"{symbol} {meta['expiry_date']} {meta['strike_raw']} {meta['option_type']}"
            if meta["option_type"] in ("CE", "PE")
            else f"{symbol} {meta['expiry_date']} FUT"
        )
        new_specs.append({
            "fin_id":        fid,
            "name":          name,
            "inst_kind":     inst_kind,
            "underlying_id": underlying_id,
            "symbol":        symbol,
            "instr_type":    meta["instr_type"],
            "expiry_date":   meta["expiry_date"],
            "strike_paise":  meta["strike_paise"],
            "option_type":   meta["option_type"],
            "lot_size":      meta["lot_size"],
        })

    # Batch UPDATE nse_fininstrmid for linked contracts
    if link_updates:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE instrument_derivatives SET nse_fininstrmid=:fid WHERE instrument_id=:iid"),
                link_updates,
            )

    return linked, new_specs


def _stats(synced, failed, rows, errors):
    return {
        "source":            SOURCE,
        "files_synced":      synced,
        "files_failed":      failed,
        "total_rows_synced": rows,
        "errors":            errors,
    }
