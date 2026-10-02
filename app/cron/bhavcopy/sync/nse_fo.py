"""
NSE F&O bhavcopy parser.

Reads downloaded BhavCopy_NSE_FO_*_F_0000.csv files.

Lookup:  FinInstrmId -> instrument_derivatives.nse_bse_id (exchange='NSE') -> instr_id
On miss: bulk-create all missing contracts.
Writes:  fo_eod (exchange='NSE')

All DB access for one file goes through a single arthdesk_db.Database,
constructed via app.database.db_session() — one connection/transaction per
file (covering every chunk), committed once, instead of a separate one per
chunk/step.
"""
from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd
from sqlalchemy import text

from app.database import db_session
from app.cron.bhavcopy.sync.base import (
    get_pending_files, load_file_chunks, mark_synced, mark_failed,
    to_int, to_float,
    bulk_resolve_fo_nse, bulk_create_fo,
    bulk_resolve_underlying_symbols,
    get_or_create_index,
    upsert_latest_prices,
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
    with db_session() as db:
        files = get_pending_files(db, SOURCE)
    if not files:
        return _stats(0, 0, 0, [])

    total_rows   = 0
    files_synced = 0
    files_failed = 0
    errors       = []

    for f in files:
        try:
            with db_session() as db:
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
    total = 0
    for i, chunk in enumerate(load_file_chunks(trade_date_str, file_name), 1):
        chunk.columns = chunk.columns.str.strip()
        rows = _process_chunk(db, chunk, trade_date_str, file_name)
        logger.debug("[%s] %s chunk %d — %d rows", SOURCE, file_name, i, rows)
        total += rows
    return total


def _process_chunk(db, df: pd.DataFrame, trade_date_str: str, file_name: str) -> int:
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

    # -- Pass 2: ONE SELECT for all unique fin_ids in this chunk, scoped to NSE --
    unique_fin_ids = df["fin_id_parsed"].unique().tolist()
    id_map = bulk_resolve_fo_nse(db, unique_fin_ids)

    # -- Pass 3: batch-resolve underlyings, bulk-create whatever NSE doesn't have yet
    missing_ids = [fid for fid in unique_fin_ids if fid not in id_map]
    if missing_ids:
        new_specs = _build_new_specs(db, missing_ids, df)
        if new_specs:
            id_map.update(bulk_create_fo(db, new_specs, "NSE"))

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
            "instr_id":          row.inst_id_mapped,
            "exchange":          EXCHANGE,
            "trade_date":        trade_date,
            "open_price":        to_float(getattr(row, "OpnPric", None)),
            "high_price":        to_float(getattr(row, "HghPric", None)),
            "low_price":         to_float(getattr(row, "LwPric", None)),
            "close_price":       to_float(getattr(row, "ClsPric", None)),
            "last_price":        to_float(getattr(row, "LastPric", None)),
            "prev_close_price":  to_float(getattr(row, "PrvsClsgPric", None)),
            "underlying_price":  to_float(getattr(row, "UndrlygPric", None)),
            "settlement_price":  to_float(getattr(row, "SttlmPric", None)),
            "open_interest":     to_int(getattr(row, "OpnIntrst", None)),
            "oi_change":         to_int(getattr(row, "ChngInOpnIntrst", None)),
            "volume":            to_int(getattr(row, "TtlTradgVol", None)),
            "traded_value_rupees": to_float(getattr(row, "TtlTrfVal", None)),
            "num_trades":        to_int(getattr(row, "TtlNbOfTxsExctd", None)),
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


def _build_new_specs(db, missing_ids: list[int], df: pd.DataFrame) -> list[dict]:
    """
    Batch-resolve underlyings for all missing fin_ids (one lookup per unique
    symbol, not per fin_id), then build the create-spec for each.

    No cross-exchange contract-matching here anymore (removed, along with
    bulk_resolve_fo_contracts_by_underlying in base.py) — NSE and BSE
    contracts are separate rows by design now (exchange discriminator), so
    there's no "maybe BSE already created this" row to find and link to.
    bulk_resolve_fo_nse already told us this exact fin_id doesn't exist on
    NSE; that's the only check needed before creating.
    """
    missing_set = set(missing_ids)

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
        return []

    unique_symbols = list({m["symbol"] for m in fid_meta.values()})
    symbol_map = bulk_resolve_underlying_symbols(db, unique_symbols)
    for sym in unique_symbols:
        if sym not in symbol_map:
            symbol_map[sym] = get_or_create_index(db, sym, EXCHANGE)

    new_specs: list[dict] = []
    for fid, meta in fid_meta.items():
        underlying_id = symbol_map[meta["symbol"]]
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

    return new_specs


def _stats(synced, failed, rows, errors):
    return {
        "source":            SOURCE,
        "files_synced":      synced,
        "files_failed":      failed,
        "total_rows_synced": rows,
        "errors":            errors,
    }
