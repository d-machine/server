"""
AMFI NAV bhavcopy parser.

Reads downloaded BhavCopy_AMFI_NAV_*_F_0000.csv files (auto-detects delimiter).

Lookup:  Scheme Code -> instrument_mf.amfi_code -> instrument_id
On miss: bulk-create all missing MF instruments in ONE transaction.
Writes:  mf_nav
"""
from __future__ import annotations

import logging
from typing import Optional

import pandas as pd
from sqlalchemy import text

from app.database import engine
from app.cron.bhavcopy.sync.base import (
    get_pending_files, load_file_bytes, mark_synced, mark_failed,
    to_paise,
    bulk_resolve_amfi, bulk_create_mf,
)

logger = logging.getLogger(__name__)
SOURCE = "AMFI_NAV"


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
    raw   = load_file_bytes(trade_date_str, file_name)
    lines = raw.decode("utf-8", errors="replace").splitlines(keepends=True)
    if not lines:
        return 0

    header_line = lines[0]
    sep = ";" if header_line.count(";") >= header_line.count(",") else ","
    n   = header_line.count(sep)
    valid_lines = [l for l in lines if l.count(sep) == n]
    if not valid_lines:
        return 0

    from io import StringIO
    df = pd.read_csv(StringIO("".join(valid_lines)), sep=sep, dtype=str)
    df.columns = df.columns.str.strip()

    scheme_col = _find_col(df, ["Scheme Code", "Code"])
    nav_col    = _find_col(df, ["Net Asset Value", "NAV"])
    date_col   = _find_col(df, ["Date", "NAV Date", "Nav Date"])
    name_col   = _find_col(df, ["Scheme Name", "Scheme", "Name"])

    if not scheme_col or not nav_col:
        raise ValueError(f"Cannot find Scheme Code or NAV column. Columns: {list(df.columns)}")

    # -- Pass 1: collect unique codes and parsed rows --------------------------
    code_meta: dict[str, str] = {}   # code -> scheme_name
    parsed_rows = []

    for _, row in df.iterrows():
        code = str(row.get(scheme_col, "")).strip()
        if not code or code == "nan":
            continue
        nav_paise = to_paise(row.get(nav_col))
        if nav_paise is None:
            continue

        nav_date = trade_date_str
        if date_col:
            parsed = _parse_amfi_date(str(row.get(date_col, "")).strip())
            if parsed:
                nav_date = parsed

        scheme_name = str(row.get(name_col, code)).strip() if name_col else code
        if code not in code_meta:
            code_meta[code] = scheme_name
        parsed_rows.append((code, nav_paise, nav_date))

    if not parsed_rows:
        return 0

    # -- Pass 2: ONE SELECT for all codes --------------------------------------
    all_codes = list(code_meta.keys())
    id_map    = bulk_resolve_amfi(all_codes)

    # -- Pass 3: bulk-create missing MF instruments in ONE transaction ---------
    missing_codes = [c for c in all_codes if c not in id_map]
    if missing_codes:
        new_specs = [{"amfi_code": c, "name": code_meta[c]} for c in missing_codes]
        new_ids   = bulk_create_mf(new_specs)
        id_map.update(new_ids)

    # -- Pass 4: build mf_nav batch --------------------------------------------
    batch   = []
    skipped = 0
    for code, nav_paise, nav_date in parsed_rows:
        inst_id = id_map.get(code)
        if inst_id is None:
            skipped += 1
            continue
        batch.append({
            "instrument_id": inst_id,
            "nav_date":      nav_date,
            "nav_paise":     nav_paise,
        })

    if batch:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO mf_nav (instrument_id, nav_date, nav_paise)
                VALUES (:instrument_id, :nav_date, :nav_paise)
                ON CONFLICT(instrument_id, nav_date) DO UPDATE SET
                    nav_paise=excluded.nav_paise
            """), batch)

    if skipped:
        logger.debug("[%s] skipped %d rows", SOURCE, skipped)

    return len(batch)


def _find_col(df: pd.DataFrame, candidates: list[str]) -> Optional[str]:
    for c in candidates:
        if c in df.columns:
            return c
    lower_map = {col.lower(): col for col in df.columns}
    for c in candidates:
        if c.lower() in lower_map:
            return lower_map[c.lower()]
    return None


def _parse_amfi_date(val: str) -> Optional[str]:
    for fmt in ("%d-%b-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            from datetime import datetime
            return datetime.strptime(val.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _stats(synced, failed, rows, errors):
    return {
        "source":            SOURCE,
        "files_synced":      synced,
        "files_failed":      failed,
        "total_rows_synced": rows,
        "errors":            errors,
    }
