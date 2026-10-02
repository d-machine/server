"""
Initialize the server SQLite database schema.

Fresh install:
    python -m app.db_init

NOTE: This release replaces daily_prices / nav_history / instrument_derivatives.
If you have an existing database, delete data/portfolio_server.db and re-run.

Instrument-identity/detail/price tables (instruments, instrument_equity,
instrument_index, instrument_derivatives, instrument_mf, instrument_mcx,
instrument_fixed_income, instrument_pending, latest_prices, equity_eod,
fo_eod, mcx_eod, mf_nav) all come from arthdesk_instruments.init_schema()
now, not from SCHEMA_SQL below — see app/tables.py.
"""

from app.database import engine
from sqlalchemy import text

from arthdesk_instruments import init_schema

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
SCHEMA_SQL = [

# -- Reference tables --------------------------------------------------------
"""CREATE TABLE IF NOT EXISTS exchanges (
    code    TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    country TEXT NOT NULL DEFAULT 'IN'
)""",

# Every instrument-identity/detail/price table (instruments, instrument_equity,
# instrument_index, instrument_derivatives, instrument_mf, instrument_mcx,
# instrument_fixed_income, instrument_pending, latest_prices, equity_eod,
# fo_eod, mcx_eod, mf_nav) is created and seeded by init_schema() below, from
# the shared arthdesk_instruments package — not here, so the server and
# arthdesk-py's client can't drift apart on them again the way
# bse_symbol/MF/instrument_type did.

# -- Operational tables ------------------------------------------------------
"""CREATE TABLE IF NOT EXISTS bhavcopy_files (
    id          INTEGER PRIMARY KEY,
    file_name   TEXT    NOT NULL UNIQUE,
    trade_date  TEXT    NOT NULL,
    source      TEXT    NOT NULL,
    status      INTEGER NOT NULL DEFAULT 1,  -- 1=downloaded 2=download_failed 3=synced 4=sync_failed
    rows_synced INTEGER,
    error       TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
)""",

"""CREATE TABLE IF NOT EXISTS trading_calendar (
    trade_date      TEXT NOT NULL,
    exchange        TEXT NOT NULL REFERENCES exchanges(code),
    is_trading_day  INTEGER NOT NULL DEFAULT 1,
    description     TEXT,
    PRIMARY KEY (trade_date, exchange)
)""",

]  # end SCHEMA_SQL

# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------
INDEX_SQL = [
    # instruments.instr_type_id, instrument_equity.{isin,nse_id,bse_id,nse_sym,bse_sym}, and
    # instrument_derivatives.{nse_bse_id,ul_instr_id} are all now indexed directly by
    # arthdesk_instruments' Table definitions (UNIQUE constraints or explicit index=True) —
    # created by init_schema(), not here. instrument_mcx has no DAO/usage yet (deferred).
    "CREATE INDEX IF NOT EXISTS idx_equity_eod_date ON equity_eod(trade_date)",
    "CREATE INDEX IF NOT EXISTS idx_fo_eod_date ON fo_eod(trade_date)",
    "CREATE INDEX IF NOT EXISTS idx_mcx_eod_date ON mcx_eod(trade_date)",
    "CREATE INDEX IF NOT EXISTS idx_mf_nav_date ON mf_nav(nav_date)",
    "CREATE INDEX IF NOT EXISTS idx_bhavcopy_source_status ON bhavcopy_files(source, status)",
    "CREATE INDEX IF NOT EXISTS idx_bhavcopy_date_status ON bhavcopy_files(trade_date, status)",
]

# Triggers that bumped instruments.updated_at on extension-table changes are
# gone — the new hub `instruments` table has no updated_at column at all
# (each detail table tracks its own now, per arthdesk_instruments' design).

# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------
SEED_SQL = [
    "INSERT OR IGNORE INTO exchanges (code, name) VALUES ('NSE',  'National Stock Exchange')",
    "INSERT OR IGNORE INTO exchanges (code, name) VALUES ('BSE',  'Bombay Stock Exchange')",
    "INSERT OR IGNORE INTO exchanges (code, name) VALUES ('MCX',  'Multi Commodity Exchange')",
    "INSERT OR IGNORE INTO exchanges (code, name) VALUES ('AMFI', 'Association of Mutual Funds in India')",

    # asset_classes / tax_categories / instrument_types seed data lives in
    # arthdesk_instruments now — see init_schema() below.
]


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------
def init():
    import os
    os.makedirs("data", exist_ok=True)

    with engine.begin() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))
        conn.execute(text("PRAGMA foreign_keys=ON"))

    # init_schema manages its own transaction (metadata.create_all + a seed
    # insert inside engine.begin()) — called with the real engine, not a
    # connection already inside another transaction.
    init_schema(engine)

    with engine.begin() as conn:
        for stmt in SCHEMA_SQL:
            conn.execute(text(stmt))
        for stmt in INDEX_SQL:
            conn.execute(text(stmt))
        for stmt in SEED_SQL:
            conn.execute(text(stmt))

    print("Database initialized at data/portfolio_server.db")


if __name__ == "__main__":
    init()
