"""
Scan downloaded bhavcopy files on disk and register them in bhavcopy_files table.

For every weekday from START_DATE to END_DATE, generates the expected filename
for each source, checks if it exists on disk, and upserts into bhavcopy_files:
  - status=1 (DOWNLOADED)      if file found on disk
  - status=2 (DOWNLOAD_FAILED) if file not found

Run from server/ directory:
    python -m scripts.scan_bhavcopy_files
    python -m scripts.scan_bhavcopy_files --start 2024-01-01 --end 2026-04-26
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from app.database import engine
from app.cron.bhavcopy.constants import FileStatus
from app.cron.bhavcopy.common import BHAVCOPY_DIR

# Expected filename templates per source: {YYYYMMDD} placeholder
SOURCES = {
    "NSE_EQ":   ["BhavCopy_NSE_CM_0_0_0_{}_F_0000.csv",
                 "BhavCopy_NSE_CM_0_0_0_{}_F_0000.csv.zip"],
    "NSE_FO":   ["BhavCopy_NSE_FO_0_0_0_{}_F_0000.csv",
                 "BhavCopy_NSE_FO_0_0_0_{}_F_0000.csv.zip"],
    "BSE_EQ":   ["BhavCopy_BSE_CM_0_0_0_{}_F_0000.CSV"],
    "BSE_FO":   ["BhavCopy_BSE_FO_0_0_0_{}_F_0000.CSV"],
    "AMFI_NAV": ["BhavCopy_AMFI_NAV_0_0_0_{}_F_0000.csv"],
    "MCX":      ["BhavCopy_MCX_0_0_0_{}_F_0000.csv"],
}

# Canonical saved filename (first template = what the downloader saves as)
CANONICAL = {src: tpls[0] for src, tpls in SOURCES.items()}


def iter_weekdays(start: date, end: date):
    d = start
    while d <= end:
        if d.weekday() < 5:  # Mon-Fri
            yield d
        d += timedelta(days=1)


def file_exists(trade_date: date, source: str) -> tuple[bool, str]:
    """
    Returns (found, canonical_filename).
    Checks all filename variants (e.g. .csv and .csv.zip for NSE).
    """
    date_str = trade_date.strftime("%Y%m%d")
    day_dir  = BHAVCOPY_DIR / trade_date.isoformat()
    canonical = CANONICAL[source].format(date_str)

    for tpl in SOURCES[source]:
        fname = tpl.format(date_str)
        if (day_dir / fname).exists():
            return True, canonical

    return False, canonical


def scan(start: date, end: date, dry_run: bool = False):
    downloaded = 0
    failed     = 0
    total      = 0

    rows = []
    for trade_date in iter_weekdays(start, end):
        date_str = trade_date.strftime("%Y%m%d")
        for source in SOURCES:
            found, canonical = file_exists(trade_date, source)
            status = FileStatus.DOWNLOADED if found else FileStatus.DOWNLOAD_FAILED
            rows.append({
                "fn":     canonical,
                "td":     trade_date.isoformat(),
                "src":    source,
                "status": int(status),
                "error":  None if found else "File not found on disk",
            })
            if found:
                downloaded += 1
            else:
                failed += 1
            total += 1

    print(f"Scanned {total} file slots ({downloaded} found, {failed} missing) "
          f"for {start} → {end}")

    if dry_run:
        print("Dry run — no DB writes.")
        return

    # Bulk upsert into bhavcopy_files
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO bhavcopy_files (file_name, trade_date, source, status, error, updated_at)
            VALUES (:fn, :td, :src, :status, :error, datetime('now'))
            ON CONFLICT(file_name) DO UPDATE SET
                status   = excluded.status,
                error    = excluded.error,
                updated_at = datetime('now')
        """), rows)

    print(f"Upserted {len(rows)} rows into bhavcopy_files.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scan bhavcopy files and register in DB")
    parser.add_argument("--start", default="2026-01-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end",   default="2026-04-26", help="End date YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true", help="Print counts only, no DB writes")
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end   = date.fromisoformat(args.end)
    scan(start, end, dry_run=args.dry_run)
