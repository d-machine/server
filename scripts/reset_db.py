"""
Reset all instrument master, EOD price, and bhavcopy tracking data.

Keeps reference tables intact (exchanges, instrument_types, trading_calendar).
Run from the server/ directory:
    python -m scripts.reset_db
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from app.database import engine

# Delete order respects FK constraints:
# EOD / price tables first, then instrument detail tables, then hub, then files
TRUNCATE_ORDER = [
    "latest_prices",
    "equity_eod",
    "fo_eod",
    "mcx_eod",
    "mf_nav",
    "instrument_derivatives",
    "instrument_mcx",
    "instrument_mf",
    "instrument_equity",
    "instrument_index",
    "instrument_fixed_income",
    "instruments",
    "bhavcopy_files",
]


def reset():
    print("Resetting database — instrument master, EOD tables, bhavcopy_files...")
    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        for table in TRUNCATE_ORDER:
            conn.execute(text(f"DELETE FROM {table}"))
            print(f"  cleared: {table}")
        conn.execute(text("PRAGMA foreign_keys=ON"))
    print("Done. Reference tables (exchanges, instrument_types, trading_calendar) untouched.")


if __name__ == "__main__":
    confirm = input("This will DELETE all instrument and price data. Type 'yes' to confirm: ")
    if confirm.strip().lower() == "yes":
        reset()
    else:
        print("Aborted.")
