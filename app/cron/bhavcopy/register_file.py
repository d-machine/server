"""
CLI wrapper to manually register a single bhavcopy file from any path.
Detects the source from the filename and delegates to the correct script.

Usage:
  python -m app.cron.bhavcopy.register_file /path/to/BhavCopy_NSE_CM_0_0_0_20260101_F_0000.csv.zip
  python -m app.cron.bhavcopy.register_file /path/to/BhavCopy_BSE_FO_0_0_0_20260101_F_0000.CSV --force
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

from app.cron.bhavcopy import nse_eq, nse_fo, bse_eq, bse_fo

logger = logging.getLogger(__name__)

_ROUTER = [
    (re.compile(r"BhavCopy_NSE_CM_", re.IGNORECASE), nse_eq),
    (re.compile(r"BhavCopy_NSE_FO_", re.IGNORECASE), nse_fo),
    (re.compile(r"BhavCopy_BSE_CM_", re.IGNORECASE), bse_eq),
    (re.compile(r"BhavCopy_BSE_FO_", re.IGNORECASE), bse_fo),
]


def register(file_path: Path, force: bool = False) -> bool:
    for pattern, module in _ROUTER:
        if pattern.search(file_path.name):
            return module.register(file_path, force=force)
    print(
        f"ERROR: Could not determine source from filename: {file_path.name}\n"
        f"Expected one of:\n"
        f"  BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip\n"
        f"  BhavCopy_NSE_FO_0_0_0_YYYYMMDD_F_0000.csv.zip\n"
        f"  BhavCopy_BSE_CM_0_0_0_YYYYMMDD_F_0000.CSV\n"
        f"  BhavCopy_BSE_FO_0_0_0_YYYYMMDD_F_0000.CSV"
    )
    return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Register a bhavcopy file manually")
    parser.add_argument("file", help="Path to the original NSE/BSE bhavcopy file")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite destination if already exists")
    args = parser.parse_args()
    ok = register(Path(args.file).resolve(), force=args.force)
    sys.exit(0 if ok else 1)
