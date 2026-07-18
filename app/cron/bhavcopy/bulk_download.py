"""
Bulk historical bhavcopy downloader.

Runs the appropriate per-source download script for each date in a range,
with a randomised 1-2 minute gap between requests to avoid rate limiting.
Skips weekends automatically. Non-trading days (holidays) will log a failure
from the source script and continue — they are not retried.

Usage:
    python -m app.cron.bhavcopy.bulk_download --source NSE_EQ --start 2024-01-01 --end 2024-12-31
    python -m app.cron.bhavcopy.bulk_download --source BSE_EQ --start 2024-01-01
    python -m app.cron.bhavcopy.bulk_download --source NSE_FO --start 2024-01-01 --end 2024-12-31 --force

Sources: NSE_EQ, NSE_FO, BSE_EQ, BSE_FO, AMFI_NAV, MCX
--end defaults to yesterday if omitted.
--force re-downloads even if already marked downloaded/synced.
"""
from __future__ import annotations

import argparse
import logging
import random
import time
from datetime import date, datetime, timedelta

from app.cron.bhavcopy import nse_eq, nse_fo, bse_eq, bse_fo, amfi, mcx

logger = logging.getLogger(__name__)

SOURCES = {
    "NSE_EQ":   nse_eq.download,
    "NSE_FO":   nse_fo.download,
    "BSE_EQ":   bse_eq.download,
    "BSE_FO":   bse_fo.download,
    "AMFI_NAV": amfi.download,
    "MCX":      mcx.download,
}

# Gap between downloads: 4-6 minutes (seconds)
GAP_MIN = 60   # default min, used only when running as CLI script
GAP_MAX = 120  # default max, used only when running as CLI script


def date_range(start: date, end: date):
    """Yield each calendar date from start to end inclusive."""
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def bulk_download(source: str, start: date, end: date, force: bool = False):
    download_fn = SOURCES[source]
    dates = list(date_range(start, end))
    total = len(dates)

    logger.info(f"=== Bulk download: {source} | {start} → {end} | {total} trading days ===")

    ok = failed = skipped = 0

    for i, trade_date in enumerate(dates, 1):
        logger.info(f"[{i}/{total}] {source} {trade_date}")
        success = download_fn(trade_date, force=force)

        if success:
            ok += 1
        else:
            failed += 1

        # Sleep between downloads, but not after the last one
        if i < total:
            gap = random.randint(GAP_MIN, GAP_MAX)
            logger.info(f"  Sleeping {gap}s before next download...")
            time.sleep(gap)

    logger.info(f"=== Done: {ok} ok, {failed} failed, {skipped} skipped out of {total} ===")
    return {"ok": ok, "failed": failed, "total": total}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Bulk historical bhavcopy downloader")
    parser.add_argument("--source", required=True, choices=SOURCES.keys(),
                        help="Which bhavcopy to download: NSE_EQ, NSE_FO, BSE_EQ, BSE_FO, AMFI_NAV, MCX")
    parser.add_argument("--start", required=True,
                        help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default=None,
                        help="End date YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--force", action="store_true",
                        help="Re-download even if already marked downloaded/synced")
    args = parser.parse_args()

    start_date = datetime.strptime(args.start, "%Y-%m-%d").date()
    end_date   = (datetime.strptime(args.end, "%Y-%m-%d").date()
                  if args.end else date.today() - timedelta(days=1))

    if start_date > end_date:
        parser.error(f"--start {start_date} is after --end {end_date}")

    bulk_download(args.source, start_date, end_date, force=args.force)
