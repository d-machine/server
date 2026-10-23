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
    bulk_resolve_fo_nse_full, bulk_create_fo,
    bulk_resolve_underlying_symbols,
    get_or_create_index,
    upsert_latest_prices,
    instrument_write_lock,
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

    # -- Pass 2: metadata for every fin_id up front -- needed both to
    # validate an id-based hit (Pass 3) and to build create-specs for a
    # genuine miss, so it's computed once here rather than twice.
    unique_fin_ids = df["fin_id_parsed"].unique().tolist()
    fid_meta = _build_fid_meta(db, unique_fin_ids, df)

    # -- Pass 3: ONE SELECT for all unique fin_ids in this chunk, scoped to
    # NSE -- then VALIDATE each hit against fid_meta rather than trusting
    # the id alone. NSE recycles FinInstrmId for an unrelated contract once
    # the original expires (observed in practice), so a row found by id can
    # be a stale match for a completely different contract; accepting it
    # as-is would silently write today's trade data onto someone else's
    # instrument. A mismatch is treated as a miss, same as if the id had
    # never been seen -- falls through to Pass 4's find-or-create (which
    # resolves by composite key, so it reuses the right existing row if
    # one exists under a different id, or creates a fresh one).
    raw_hits = bulk_resolve_fo_nse_full(db, unique_fin_ids)
    id_map: dict[int, int] = {}
    missing_ids: list[int] = []
    for fid in unique_fin_ids:
        meta = fid_meta.get(fid)
        if meta is None:
            continue   # unparseable row (e.g. bad expiry) -- already warned in _build_fid_meta
        stored = raw_hits.get(fid)
        if stored is not None and _row_matches(stored, meta):
            id_map[fid] = stored["instr_id"]
        else:
            missing_ids.append(fid)

    # -- Pass 4: bulk-create (or composite-key-match) whatever's missing ---
    if missing_ids:
        new_specs = _specs_from_meta(fid_meta, missing_ids)
        if new_specs:
            id_map.update(bulk_create_fo(db, new_specs, "NSE"))

    # -- Pass 5: map instrument_ids vectorized --------------------------------
    df["inst_id_mapped"] = df["fin_id_parsed"].map(id_map)
    skipped += int(df["inst_id_mapped"].isna().sum())
    df = df[df["inst_id_mapped"].notna()].copy()
    if df.empty:
        if skipped:
            logger.debug("[%s] %s -- skipped %d rows", SOURCE, file_name, skipped)
        return 0
    df["inst_id_mapped"] = df["inst_id_mapped"].astype(int)

    # -- Pass 6: build fo_eod batch with itertuples ---------------------------
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


def _build_fid_meta(db, fin_ids: list[int], df: pd.DataFrame) -> dict[int, dict]:
    """
    Parse + resolve full contract metadata for EVERY given fin_id, not just
    ones missing from the fast id-based lookup -- needed to validate an
    id-based hit (_row_matches) as well as to build the create-spec for a
    genuine miss, so this always runs once per chunk rather than being
    skipped for ids the fast path thinks it already knows.

    Returns {fin_id: {symbol, instr_type, expiry_date, strike_raw,
    strike_paise, option_type, lot_size, underlying_id}}. A fin_id with an
    unparseable expiry is omitted (warned, not raised) -- same as before.
    """
    fid_set = set(fin_ids)

    fid_meta: dict[int, dict] = {}
    subset = df[df["fin_id_parsed"].isin(fid_set)].drop_duplicates("fin_id_parsed")
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
        return {}

    unique_symbols = list({m["symbol"] for m in fid_meta.values()})
    symbol_map = bulk_resolve_underlying_symbols(db, unique_symbols)
    for sym in unique_symbols:
        if sym not in symbol_map:
            symbol_map[sym] = get_or_create_index(db, sym, EXCHANGE)

    for meta in fid_meta.values():
        meta["underlying_id"] = symbol_map[meta["symbol"]]

    return fid_meta


def _row_matches(stored: dict, meta: dict) -> bool:
    """
    True if a row found by nse_bse_id still represents the SAME contract
    the current file's row describes for that id -- i.e. the exchange
    hasn't recycled this id for an unrelated contract since it was created.
    Compares the identity fields that actually define a contract
    (underlying, expiry, strike, option type); created_at/lot_size etc.
    are irrelevant to identity and not compared.
    """
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
        name = (
            f"{symbol} {meta['expiry_date']} {meta['strike_raw']} {meta['option_type']}"
            if meta["option_type"] in ("CE", "PE")
            else f"{symbol} {meta['expiry_date']} FUT"
        )
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
