"""
BSE Equity bhavcopy parser.

Reads downloaded BhavCopy_BSE_CM_*_F_0000.CSV files.
Bulk-resolves instrument_id by ISIN; auto-creates missing instruments.
Writes to equity_eod (exchange='BSE') and latest_prices.

BSE bhavcopy columns used for instrument master:
  ISIN, TckrSymb (BSE's own ticker -> bse_sym), FinInstrmNm (name),
  FinInstrmId (BSE's own numeric scrip code -> bse_id, parallel to NSE's
  nse_id — confirmed same FinInstrmId/ISIN/TckrSymb header shape as NSE).

All DB access for one file goes through a single arthdesk_db.Database,
constructed via app.database.db_session() — one connection/transaction per
file, committed once, instead of a separate one per step.
"""
from __future__ import annotations

import logging

import pandas as pd
from sqlalchemy import text

from app.database import db_session
from app.cron.bhavcopy.sync.base import (
    get_pending_files, load_file_df, mark_synced, mark_failed,
    to_int, to_float,
    bulk_resolve_equity, bulk_create_equity, bulk_update_equity_fields,
    batch_upsert_latest_prices,
)

logger = logging.getLogger(__name__)
SOURCE   = "BSE_EQ"
EXCHANGE = "BSE"


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
    df = load_file_df(trade_date_str, file_name, dtype=str)
    df.columns = df.columns.str.strip()

    # -- Pass 1: collect unique ISINs and instrument metadata --------------------
    isin_meta: dict[str, dict] = {}
    valid_rows = []

    for _, row in df.iterrows():
        isin = str(row.get("ISIN", "")).strip()
        if not isin or isin == "nan":
            continue
        c = to_float(row.get("ClsPric"))
        if c is None:
            continue
        bse_id   = to_int(row.get("FinInstrmId"))
        bse_sym  = str(row.get("TckrSymb", "")).strip() or None
        name     = str(row.get("FinInstrmNm", bse_sym or isin)).strip() or isin
        if isin not in isin_meta:
            isin_meta[isin] = {"name": name, "bse_id": bse_id, "bse_sym": bse_sym}
        valid_rows.append(row)

    if not valid_rows:
        return 0

    # -- Pass 2: ONE SELECT for all ISINs ----------------------------------------
    all_isins = list(isin_meta.keys())
    id_map    = bulk_resolve_equity(db, all_isins)

    # -- Pass 3: bulk-create missing instruments ---------------------------------
    missing = [
        {"isin": isin, **isin_meta[isin]}
        for isin in all_isins if isin not in id_map
    ]
    if missing:
        new_ids = bulk_create_equity(db, missing)
        id_map.update(new_ids)

    # -- Pass 4: fill in missing fields on existing instruments ------------------
    new_isins = {m["isin"] for m in missing}
    updates   = []
    for isin, meta in isin_meta.items():
        inst_id = id_map.get(isin)
        if inst_id and isin not in new_isins:
            upd = {"instrument_id": inst_id}
            if meta.get("bse_id") is not None:
                upd["bse_id"] = meta["bse_id"]
            if meta.get("bse_sym"):
                upd["bse_code"] = meta["bse_sym"]  # bulk_update_equity_fields' legacy key name
            if len(upd) > 1:
                updates.append(upd)
    if updates:
        bulk_update_equity_fields(db, updates)

    # -- Pass 5: build EOD batch -------------------------------------------------
    eod_batch    = []
    latest_batch = []
    skipped      = 0

    for row in valid_rows:
        isin    = str(row.get("ISIN", "")).strip()
        inst_id = id_map.get(isin)
        if inst_id is None:
            skipped += 1
            continue

        trade_date = str(row.get("TradDt", trade_date_str)).strip()[:10]
        o = to_float(row.get("OpnPric"))
        h = to_float(row.get("HghPric"))
        l = to_float(row.get("LwPric"))
        c = to_float(row.get("ClsPric"))
        if c is None:
            skipped += 1
            continue

        eod_batch.append({
            "instr_id":            inst_id,
            "exchange":            EXCHANGE,
            "trade_date":          trade_date,
            "series":              str(row.get("SctySrs", "")).strip() or None,
            "open_price":          o,
            "high_price":          h,
            "low_price":           l,
            "close_price":         c,
            "last_price":          to_float(row.get("LastPric")),
            "prev_close_price":    to_float(row.get("PrvsClsgPric")),
            "settlement_price":    to_float(row.get("SttlmPric")),
            "volume":              to_int(row.get("TtlTradgVol")),
            "traded_value_rupees": to_float(row.get("TtlTrfVal")),
            "num_trades":          to_int(row.get("TtlNbOfTxsExctd")),
        })
        latest_batch.append({
            "instrument_id":     inst_id,
            "exchange":          EXCHANGE,
            "price_date":        trade_date,
            "open_price_paise":  int(round(o * 100)) if o is not None else None,
            "high_price_paise":  int(round(h * 100)) if h is not None else None,
            "low_price_paise":   int(round(l * 100)) if l is not None else None,
            "close_price_paise": int(round(c * 100)),
        })

    # -- Pass 6: batch upserts ---------------------------------------------------
    if eod_batch:
        db.execute(text("""
            INSERT INTO equity_eod (
                instr_id, exchange, trade_date, series,
                open_price, high_price, low_price,
                close_price, last_price, prev_close_price,
                settlement_price, volume, traded_value_rupees, num_trades
            ) VALUES (
                :instr_id, :exchange, :trade_date, :series,
                :open_price, :high_price, :low_price,
                :close_price, :last_price, :prev_close_price,
                :settlement_price, :volume, :traded_value_rupees, :num_trades
            )
            ON CONFLICT(instr_id, exchange, trade_date) DO UPDATE SET
                series=excluded.series,
                open_price=excluded.open_price,
                high_price=excluded.high_price,
                low_price=excluded.low_price,
                close_price=excluded.close_price,
                last_price=excluded.last_price,
                prev_close_price=excluded.prev_close_price,
                settlement_price=excluded.settlement_price,
                volume=excluded.volume,
                traded_value_rupees=excluded.traded_value_rupees,
                num_trades=excluded.num_trades
        """), eod_batch)

    # latest_prices is deferred (dao.prices not built yet) — don't let its
    # now-broken schema block the in-scope equity_eod write above.
    try:
        batch_upsert_latest_prices(latest_batch)
    except Exception:
        logger.exception("[%s] latest_prices upsert failed (deferred, not migrated yet)", SOURCE)

    if skipped:
        logger.debug("[%s] %s -- skipped %d rows (missing ISIN or close price)", SOURCE, file_name, skipped)

    return len(eod_batch)


def _stats(synced, failed, rows, errors):
    return {
        "source":            SOURCE,
        "files_synced":      synced,
        "files_failed":      failed,
        "total_rows_synced": rows,
        "errors":            errors,
    }
