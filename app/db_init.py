"""
Initialize the server SQLite database schema.

Fresh install:
    python -m app.db_init

NOTE: This release replaces daily_prices / nav_history / instrument_derivatives.
If you have an existing database, delete data/portfolio_server.db and re-run.
"""

from app.database import engine
from sqlalchemy import text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from arthdesk_instruments import (
    metadata as shared_metadata,
    asset_classes,
    tax_categories,
    instrument_types,
    ASSET_CLASSES_SEED,
    TAX_CATEGORIES_SEED,
    INSTRUMENT_TYPES_SEED,
)

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

# asset_classes / tax_categories / instrument_types are created and seeded by
# _create_shared_instrument_tables() below, from the shared arthdesk_instruments
# package — not here, so the server and arthdesk-py's client can't drift apart
# on them again the way bse_symbol/MF/instrument_type did.

# -- Hub table ---------------------------------------------------------------
# Thin: only fields universal across every asset class. isin is additive here
# for parity with the client's hub table (which has always had it) — the
# server's own per-type tables (instrument_equity etc.) remain the primary
# place isin lives for actual lookups; this is a nullable convenience column.
"""CREATE TABLE IF NOT EXISTS instruments (
    instrument_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name               TEXT NOT NULL,
    instrument_type_id INTEGER NOT NULL REFERENCES instrument_types(instrument_type_id),
    isin               TEXT UNIQUE,
    is_active          INTEGER NOT NULL DEFAULT 1,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT NOT NULL DEFAULT (datetime('now'))
)""",

# -- Instrument detail tables (1-to-1 with instruments) ----------------------
"""CREATE TABLE IF NOT EXISTS instrument_equity (
    instrument_id       INTEGER PRIMARY KEY REFERENCES instruments(instrument_id),
    isin                TEXT UNIQUE,
    nse_symbol          TEXT,
    nse_fininstrmid     INTEGER,
    bse_code            TEXT,
    face_value_paise    INTEGER,
    sector              TEXT,
    industry            TEXT
)""",

"""CREATE TABLE IF NOT EXISTS instrument_index (
    instrument_id   INTEGER PRIMARY KEY REFERENCES instruments(instrument_id),
    symbol          TEXT NOT NULL,
    exchange        TEXT NOT NULL REFERENCES exchanges(code),
    UNIQUE(symbol, exchange)
)""",

"""CREATE TABLE IF NOT EXISTS instrument_mf (
    instrument_id   INTEGER PRIMARY KEY REFERENCES instruments(instrument_id),
    isin            TEXT UNIQUE,
    amfi_code       TEXT UNIQUE,
    scheme_type     TEXT,
    fund_house      TEXT,
    plan            TEXT,
    option          TEXT
)""",

"""CREATE TABLE IF NOT EXISTS instrument_fixed_income (
    instrument_id       INTEGER PRIMARY KEY REFERENCES instruments(instrument_id),
    isin                TEXT UNIQUE,
    interest_rate_bps   INTEGER,
    maturity_date       TEXT,
    compounding         TEXT,
    issuer              TEXT
)""",

# F&O: one record per contract, exchange-agnostic.
# nse_fininstrmid / bse_fininstrmid used for lookup from respective bhavcopy.
# option_type = '-' for futures (avoids NULL in UNIQUE key).
# strike_price_paise = 0 for futures.
"""CREATE TABLE IF NOT EXISTS instrument_derivatives (
    instrument_id            INTEGER PRIMARY KEY REFERENCES instruments(instrument_id),
    underlying_instrument_id INTEGER REFERENCES instruments(instrument_id),
    underlying_symbol        TEXT NOT NULL,
    contract_type            TEXT NOT NULL,
    expiry_date              TEXT NOT NULL,
    strike_price_paise       INTEGER NOT NULL DEFAULT 0,
    option_type              TEXT NOT NULL DEFAULT '-',
    lot_size                 INTEGER,
    nse_fininstrmid          INTEGER UNIQUE,
    bse_fininstrmid          INTEGER UNIQUE,
    UNIQUE(underlying_instrument_id, expiry_date, strike_price_paise, option_type)
)""",

"""CREATE TABLE IF NOT EXISTS instrument_mcx (
    instrument_id      INTEGER PRIMARY KEY REFERENCES instruments(instrument_id),
    mcx_symbol         TEXT NOT NULL,
    contract_type      TEXT NOT NULL,
    expiry_date        TEXT NOT NULL,
    strike_price_paise INTEGER NOT NULL DEFAULT 0,
    option_type        TEXT NOT NULL DEFAULT '-',
    lot_size           REAL,
    unit               TEXT,
    UNIQUE(mcx_symbol, contract_type, expiry_date, strike_price_paise, option_type)
)""",

# -- EOD price tables --------------------------------------------------------
"""CREATE TABLE IF NOT EXISTS equity_eod (
    instrument_id           INTEGER NOT NULL REFERENCES instruments(instrument_id),
    exchange                TEXT    NOT NULL REFERENCES exchanges(code),
    trade_date              TEXT    NOT NULL,
    series                  TEXT,
    open_price_paise        INTEGER,
    high_price_paise        INTEGER,
    low_price_paise         INTEGER,
    close_price_paise       INTEGER NOT NULL,
    last_price_paise        INTEGER,
    prev_close_paise        INTEGER,
    settlement_price_paise  INTEGER,
    volume                  INTEGER,
    traded_value_rupees     REAL,
    num_trades              INTEGER,
    PRIMARY KEY (instrument_id, exchange, trade_date)
)""",

"""CREATE TABLE IF NOT EXISTS fo_eod (
    instrument_id           INTEGER NOT NULL REFERENCES instruments(instrument_id),
    exchange                TEXT    NOT NULL REFERENCES exchanges(code),
    trade_date              TEXT    NOT NULL,
    open_price_paise        INTEGER,
    high_price_paise        INTEGER,
    low_price_paise         INTEGER,
    close_price_paise       INTEGER,
    last_price_paise        INTEGER,
    prev_close_paise        INTEGER,
    underlying_price_paise  INTEGER,
    settlement_price_paise  INTEGER,
    open_interest           INTEGER,
    oi_change               INTEGER,
    volume                  INTEGER,
    traded_value_rupees     REAL,
    num_trades              INTEGER,
    PRIMARY KEY (instrument_id, exchange, trade_date)
)""",

"""CREATE TABLE IF NOT EXISTS mcx_eod (
    instrument_id       INTEGER NOT NULL REFERENCES instruments(instrument_id),
    trade_date          TEXT    NOT NULL,
    open_price          REAL,
    high_price          REAL,
    low_price           REAL,
    close_price         REAL    NOT NULL,
    prev_close          REAL,
    volume_lots         INTEGER,
    volume_quantity     REAL,
    value_lacs          REAL,
    open_interest_lots  INTEGER,
    PRIMARY KEY (instrument_id, trade_date)
)""",

"""CREATE TABLE IF NOT EXISTS mf_nav (
    instrument_id   INTEGER NOT NULL REFERENCES instruments(instrument_id),
    nav_date        TEXT    NOT NULL,
    nav_paise       INTEGER NOT NULL,
    PRIMARY KEY (instrument_id, nav_date)
)""",

# -- Latest prices cache -----------------------------------------------------
"""CREATE TABLE IF NOT EXISTS latest_prices (
    instrument_id       INTEGER PRIMARY KEY REFERENCES instruments(instrument_id),
    exchange            TEXT,
    price_date          TEXT    NOT NULL,
    open_price_paise    INTEGER,
    high_price_paise    INTEGER,
    low_price_paise     INTEGER,
    close_price_paise   INTEGER NOT NULL,
    last_synced_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT    NOT NULL DEFAULT (datetime('now'))
)""",

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
    "CREATE INDEX IF NOT EXISTS idx_instruments_type_id ON instruments(instrument_type_id)",
    "CREATE INDEX IF NOT EXISTS idx_instrument_equity_isin ON instrument_equity(isin)",
    "CREATE INDEX IF NOT EXISTS idx_instrument_equity_nse_symbol ON instrument_equity(nse_symbol)",
    "CREATE INDEX IF NOT EXISTS idx_instrument_equity_bse_code ON instrument_equity(bse_code)",
    "CREATE INDEX IF NOT EXISTS idx_instrument_derivatives_nse_id ON instrument_derivatives(nse_fininstrmid)",
    "CREATE INDEX IF NOT EXISTS idx_instrument_derivatives_bse_id ON instrument_derivatives(bse_fininstrmid)",
    "CREATE INDEX IF NOT EXISTS idx_instrument_derivatives_underlying ON instrument_derivatives(underlying_instrument_id, expiry_date)",
    "CREATE INDEX IF NOT EXISTS idx_instrument_mcx_lookup ON instrument_mcx(mcx_symbol, contract_type, expiry_date)",
    "CREATE INDEX IF NOT EXISTS idx_equity_eod_date ON equity_eod(trade_date)",
    "CREATE INDEX IF NOT EXISTS idx_fo_eod_date ON fo_eod(trade_date)",
    "CREATE INDEX IF NOT EXISTS idx_mcx_eod_date ON mcx_eod(trade_date)",
    "CREATE INDEX IF NOT EXISTS idx_mf_nav_date ON mf_nav(nav_date)",
    "CREATE INDEX IF NOT EXISTS idx_bhavcopy_source_status ON bhavcopy_files(source, status)",
    "CREATE INDEX IF NOT EXISTS idx_bhavcopy_date_status ON bhavcopy_files(trade_date, status)",
]

# ---------------------------------------------------------------------------
# Triggers — bump instruments.updated_at when any extension table changes
# ---------------------------------------------------------------------------
TRIGGER_SQL = [
    """CREATE TRIGGER IF NOT EXISTS trig_instrument_equity_upd
       AFTER UPDATE ON instrument_equity
       BEGIN
           UPDATE instruments SET updated_at = datetime('now')
           WHERE instrument_id = NEW.instrument_id;
       END""",
    """CREATE TRIGGER IF NOT EXISTS trig_instrument_derivatives_upd
       AFTER UPDATE ON instrument_derivatives
       BEGIN
           UPDATE instruments SET updated_at = datetime('now')
           WHERE instrument_id = NEW.instrument_id;
       END""",
    """CREATE TRIGGER IF NOT EXISTS trig_instrument_mcx_upd
       AFTER UPDATE ON instrument_mcx
       BEGIN
           UPDATE instruments SET updated_at = datetime('now')
           WHERE instrument_id = NEW.instrument_id;
       END""",
    """CREATE TRIGGER IF NOT EXISTS trig_instrument_index_upd
       AFTER UPDATE ON instrument_index
       BEGIN
           UPDATE instruments SET updated_at = datetime('now')
           WHERE instrument_id = NEW.instrument_id;
       END""",
    """CREATE TRIGGER IF NOT EXISTS trig_instrument_mf_upd
       AFTER UPDATE ON instrument_mf
       BEGIN
           UPDATE instruments SET updated_at = datetime('now')
           WHERE instrument_id = NEW.instrument_id;
       END""",
    """CREATE TRIGGER IF NOT EXISTS trig_instrument_fixed_income_upd
       AFTER UPDATE ON instrument_fixed_income
       BEGIN
           UPDATE instruments SET updated_at = datetime('now')
           WHERE instrument_id = NEW.instrument_id;
       END""",
]

# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------
SEED_SQL = [
    "INSERT OR IGNORE INTO exchanges (code, name) VALUES ('NSE',  'National Stock Exchange')",
    "INSERT OR IGNORE INTO exchanges (code, name) VALUES ('BSE',  'Bombay Stock Exchange')",
    "INSERT OR IGNORE INTO exchanges (code, name) VALUES ('MCX',  'Multi Commodity Exchange')",
    "INSERT OR IGNORE INTO exchanges (code, name) VALUES ('AMFI', 'Association of Mutual Funds in India')",

    # asset_classes / instrument_types seed data lives in arthdesk_instruments now —
    # see _create_shared_instrument_tables() below.
]


# ---------------------------------------------------------------------------
# Shared instrument-identity tables (arthdesk_instruments)
# ---------------------------------------------------------------------------
def create_shared_instrument_tables(bind):
    """
    Create + seed asset_classes/tax_categories/instrument_types from the
    shared arthdesk_instruments package. Idempotent (checkfirst on create,
    INSERT OR IGNORE on seed) — safe to call on every startup, and reused
    by tests/conftest.py so the in-memory test schema matches production.
    """
    shared_metadata.create_all(bind, checkfirst=True)
    for table, seed in (
        (asset_classes, ASSET_CLASSES_SEED),
        (tax_categories, TAX_CATEGORIES_SEED),
        (instrument_types, INSTRUMENT_TYPES_SEED),
    ):
        bind.execute(sqlite_insert(table).prefix_with("OR IGNORE"), seed)


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------
def init():
    import os
    os.makedirs("data", exist_ok=True)

    with engine.begin() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))
        conn.execute(text("PRAGMA foreign_keys=ON"))
        create_shared_instrument_tables(conn)
        for stmt in SCHEMA_SQL:
            conn.execute(text(stmt))
        for stmt in INDEX_SQL:
            conn.execute(text(stmt))
        for stmt in TRIGGER_SQL:
            conn.execute(text(stmt))
        for stmt in SEED_SQL:
            conn.execute(text(stmt))

    print("Database initialized at data/portfolio_server.db")


if __name__ == "__main__":
    init()
