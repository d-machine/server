"""
MCX bhavcopy parser.

Reads downloaded BhavCopy_MCX_0_0_0_YYYYMMDD_F_0000.csv files.

Lookup:  (symbol, instrument_type, expiry_date, strike_price, option_type)
         -> instrument_mcx -> instrument_id
On miss: bulk-create all missing instruments in ONE transaction.
Writes:  mcx_eod
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

import pandas as pd
from sqlalchemy import text

from app.database import engine
from app.cron.bhavcopy.sync.base import (
    get_pending_files, load_file_df, mark_synced, mark_failed,
    to_int, to_float,
    bulk_resolve_mcx, bulk_create_mcx,
)

logger = logging.getLogger(__name__)
SOURCE  = "MCX"

_VOL_RE = re.compile(r"([\d.]+)")


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
    df = load_file_df(trade_date_str, file_name, dtype=str)
    df.columns = df.columns.str.strip()

    # -- Pass 1: parse all valid rows, collect unique keys ---------------------
    parsed_rows = []
    skipped     = 0

    for _, row in df.iterrows():
        symbol     = str(row.get("Symbol", "")).strip()
        instr_type = str(row.get("InstrumentName", "")).strip()
        expiry_raw = str(row.get("ExpiryDate", "")).strip()
        option_raw = str(row.get("OptionType", "")).strip()
        strike_raw = str(row.get("StrikePrice", "0")).strip()

        if not symbol or not expiry_raw or expiry_raw == "nan":
            skipped += 1
            continue

        expiry_date = _parse_expiry(expiry_raw)
        if expiry_date is None:
            skipped += 1
            continue

        option_type = option_raw if option_raw in ("CE", "PE") else "-"
        try:
            strike = float(strike_raw) if strike_raw and strike_raw != "nan" else 0.0
        except ValueError:
            strike = 0.0
        if option_type == "-":
            strike = 0.0

        close = to_float(row.get("Close"))
        if close is None:
            close = to_float(row.get("PreviousClose"))
        if close is None:
            skipped += 1
            continue

        trade_date = _parse_trade_date(str(row.get("DateDisplay", trade_date_str)).strip())
        if trade_date is None:
            trade_date = trade_date_str

        strike_paise = int(round(strike * 100))
        key = (symbol, instr_type, expiry_date, strike_paise, option_type)
        parsed_rows.append((key, row, close, trade_date))

    if not parsed_rows:
        return 0

    # -- Pass 2: ONE SELECT for all unique keys --------------------------------
    unique_keys = list({k for k, _, _, _ in parsed_rows})
    id_map      = bulk_resolve_mcx(unique_keys)

    # -- Pass 3: bulk-create all missing instruments in ONE transaction --------
    missing_keys = [k for k in unique_keys if k not in id_map]
    if missing_keys:
        # Build one representative row per missing key
        key_to_row: dict[tuple, object] = {}
        for key, row, _, _ in parsed_rows:
            if key in missing_keys and key not in key_to_row:
                key_to_row[key] = row

        new_specs = []
        for key, row in key_to_row.items():
            symbol, instr_type, expiry_date, strike_paise, option_type = key
            inst_kind = "COMMODITY_OPTIONS" if instr_type == "OPTFUT" else "COMMODITY_FUTURES"
            strike_display = strike_paise / 100
            name = (f"MCX {symbol} {expiry_date} {strike_display} {option_type}"
                    if option_type in ("CE", "PE")
                    else f"MCX {symbol} {expiry_date} FUT")
            new_specs.append({
                "key":         key,
                "name":        name,
                "inst_kind":   inst_kind,
                "mcx_symbol":  symbol,
                "instr_type":  instr_type,
                "expiry_date": expiry_date,
                "strike_paise": strike_paise,
                "option_type": option_type,
                "unit":        _parse_unit(str(row.get("VolumeInThousands", ""))),
            })

        if new_specs:
            new_ids = bulk_create_mcx(new_specs)
            id_map.update(new_ids)

    # -- Pass 4: build mcx_eod batch -------------------------------------------
    batch = []
    for key, row, close, trade_date in parsed_rows:
        inst_id = id_map.get(key)
        if inst_id is None:
            skipped += 1
            continue

        vol_qty = _parse_volume_qty(str(row.get("VolumeInThousands", "")))
        batch.append({
            "instrument_id":      inst_id,
            "trade_date":         trade_date,
            "open_price":         to_float(row.get("Open")) or None,
            "high_price":         to_float(row.get("High")) or None,
            "low_price":          to_float(row.get("Low")) or None,
            "close_price":        close,
            "prev_close":         to_float(row.get("PreviousClose")),
            "volume_lots":        to_int(row.get("Volume")),
            "volume_quantity":    vol_qty,
            "value_lacs":         to_float(row.get("Value")),
            "open_interest_lots": to_int(row.get("OpenInterest")),
        })

    if batch:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO mcx_eod (
                    instrument_id, trade_date,
                    open_price, high_price, low_price, close_price, prev_close,
                    volume_lots, volume_quantity, value_lacs, open_interest_lots
                ) VALUES (
                    :instrument_id, :trade_date,
                    :open_price, :high_price, :low_price, :close_price, :prev_close,
                    :volume_lots, :volume_quantity, :value_lacs, :open_interest_lots
                )
                ON CONFLICT(instrument_id, trade_date) DO UPDATE SET
                    open_price=excluded.open_price,
                    high_price=excluded.high_price,
                    low_price=excluded.low_price,
                    close_price=excluded.close_price,
                    prev_close=excluded.prev_close,
                    volume_lots=excluded.volume_lots,
                    volume_quantity=excluded.volume_quantity,
                    value_lacs=excluded.value_lacs,
                    open_interest_lots=excluded.open_interest_lots
            """), batch)

    if skipped:
        logger.debug("[%s] %s -- skipped %d rows", SOURCE, file_name, skipped)

    return len(batch)


def _parse_expiry(val: str) -> Optional[str]:
    for fmt in ("%d%b%Y", "%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(val.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    logger.warning("[%s] Cannot parse expiry date: %s", SOURCE, val)
    return None


def _parse_trade_date(val: str) -> Optional[str]:
    for fmt in ("%d %b %Y", "%d-%b-%Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(val.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _parse_volume_qty(val: str) -> Optional[float]:
    m = _VOL_RE.match(val.strip())
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


def _parse_unit(val: str) -> Optional[str]:
    parts = val.strip().split()
    return parts[-1].strip() if len(parts) >= 2 else None


def _stats(synced, failed, rows, errors):
    return {
        "source":            SOURCE,
        "files_synced":      synced,
        "files_failed":      failed,
        "total_rows_synced": rows,
        "errors":            errors,
    }
