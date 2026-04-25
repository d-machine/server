"""
Initialize the server SQLite database schema.
Run once: python -m app.db_init
"""

from app.database import engine, Base
from sqlalchemy import text


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS exchanges (
    exchange_id   INTEGER PRIMARY KEY,
    code          TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    country       TEXT NOT NULL DEFAULT 'IN'
);

CREATE TABLE IF NOT EXISTS instrument_types (
    instrument_type_id  INTEGER PRIMARY KEY,
    name                TEXT NOT NULL UNIQUE,
    asset_class         TEXT NOT NULL,
    tax_category        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS instruments (
    instrument_id       INTEGER PRIMARY KEY,
    isin                TEXT UNIQUE,
    name                TEXT NOT NULL,
    instrument_type_id  INTEGER NOT NULL REFERENCES instrument_types(instrument_type_id),
    primary_exchange_id INTEGER REFERENCES exchanges(exchange_id),
    is_active           INTEGER NOT NULL DEFAULT 1,
    source              TEXT NOT NULL DEFAULT 'SERVER',
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS instrument_equity (
    instrument_id       INTEGER PRIMARY KEY REFERENCES instruments(instrument_id),
    nse_symbol          TEXT,
    bse_code            TEXT,
    face_value_paise    INTEGER,
    sector              TEXT,
    industry            TEXT
);

CREATE TABLE IF NOT EXISTS instrument_mf (
    instrument_id   INTEGER PRIMARY KEY REFERENCES instruments(instrument_id),
    amfi_code       TEXT UNIQUE,
    scheme_type     TEXT,
    fund_house      TEXT,
    plan            TEXT,
    option          TEXT
);

CREATE TABLE IF NOT EXISTS instrument_fixed_income (
    instrument_id       INTEGER PRIMARY KEY REFERENCES instruments(instrument_id),
    interest_rate_bps   INTEGER,
    maturity_date       TEXT,
    compounding         TEXT,
    issuer              TEXT
);

CREATE TABLE IF NOT EXISTS instrument_derivatives (
    instrument_id       INTEGER PRIMARY KEY REFERENCES instruments(instrument_id),
    underlying_isin     TEXT REFERENCES instruments(isin),
    expiry_date         TEXT NOT NULL,
    lot_size            INTEGER NOT NULL,
    strike_price_paise  INTEGER,
    contract_type       TEXT
);

CREATE TABLE IF NOT EXISTS instrument_mcx (
    instrument_id   INTEGER PRIMARY KEY REFERENCES instruments(instrument_id),
    mcx_symbol      TEXT NOT NULL,
    expiry_date     TEXT NOT NULL,
    lot_size        REAL NOT NULL,
    unit            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_prices (
    price_id            INTEGER PRIMARY KEY,
    instrument_id       INTEGER NOT NULL REFERENCES instruments(instrument_id),
    trade_date          TEXT NOT NULL,
    open_price_paise    INTEGER,
    high_price_paise    INTEGER,
    low_price_paise     INTEGER,
    close_price_paise   INTEGER NOT NULL,
    volume              INTEGER,
    source              TEXT NOT NULL,
    UNIQUE(instrument_id, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_daily_prices_instrument_date
    ON daily_prices(instrument_id, trade_date);

CREATE TABLE IF NOT EXISTS nav_history (
    nav_id          INTEGER PRIMARY KEY,
    instrument_id   INTEGER NOT NULL REFERENCES instruments(instrument_id),
    nav_date        TEXT NOT NULL,
    nav_paise       INTEGER NOT NULL,
    UNIQUE(instrument_id, nav_date)
);

CREATE TABLE IF NOT EXISTS latest_prices (
    instrument_id       INTEGER PRIMARY KEY REFERENCES instruments(instrument_id),
    price_date          TEXT NOT NULL,           -- YYYY-MM-DD trading date
    open_price_paise    INTEGER,                 -- day's open
    high_price_paise    INTEGER,                 -- day's high so far
    low_price_paise     INTEGER,                 -- day's low so far
    close_price_paise   INTEGER NOT NULL,        -- current / closing price
    last_synced_at      TEXT NOT NULL DEFAULT (datetime('now')),  -- updated only when price changes
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))   -- updated on every check
);

CREATE TABLE IF NOT EXISTS trading_calendar (
    holiday_date    TEXT PRIMARY KEY,
    description     TEXT,
    exchange_id     INTEGER REFERENCES exchanges(exchange_id)
);
"""

SEED_SQL = """
INSERT OR IGNORE INTO exchanges (code, name) VALUES
    ('NSE', 'National Stock Exchange'),
    ('BSE', 'Bombay Stock Exchange'),
    ('MCX', 'Multi Commodity Exchange');

INSERT OR IGNORE INTO instrument_types (name, asset_class, tax_category) VALUES
    ('EQUITY',           'EQUITY',       'EQUITY_LTCG'),
    ('EQUITY_MF',        'MF',           'EQUITY_LTCG'),
    ('DEBT_MF',          'MF',           'DEBT'),
    ('HYBRID_MF',        'MF',           'EQUITY_LTCG'),
    ('ELSS',             'MF',           'EQUITY_LTCG'),
    ('FD',               'FIXED_INCOME', 'DEBT'),
    ('BOND',             'FIXED_INCOME', 'DEBT'),
    ('PPF',              'FIXED_INCOME', 'DEBT'),
    ('NPS',              'FIXED_INCOME', 'DEBT'),
    ('FUTURES',          'DERIVATIVE',   'NON_SPECULATIVE'),
    ('OPTIONS',          'DERIVATIVE',   'NON_SPECULATIVE'),
    ('COMMODITY_FUTURES','COMMODITY',    'NON_SPECULATIVE');
"""


def _run_migrations():
    """
    Apply schema migrations to existing DBs.
    Each migration runs in its own transaction and is silently skipped if
    the change already exists (idempotent).

    SQLite ALTER TABLE ADD COLUMN requires a *constant* default value —
    datetime('now') is not allowed.  Use a fixed epoch string instead and
    backfill meaningful values afterwards.
    """
    migrations = [
        # latest_prices: add OHLC columns
        "ALTER TABLE latest_prices ADD COLUMN open_price_paise INTEGER",
        "ALTER TABLE latest_prices ADD COLUMN high_price_paise INTEGER",
        "ALTER TABLE latest_prices ADD COLUMN low_price_paise INTEGER",
        # last_synced_at: constant default required by SQLite ADD COLUMN
        "ALTER TABLE latest_prices ADD COLUMN last_synced_at TEXT NOT NULL DEFAULT '1970-01-01T00:00:00'",
        # Backfill: set last_synced_at = updated_at for existing rows
        "UPDATE latest_prices SET last_synced_at = updated_at WHERE last_synced_at = '1970-01-01T00:00:00'",
    ]
    for sql in migrations:
        try:
            with engine.begin() as conn:
                conn.execute(text(sql))
        except Exception:
            pass   # column already exists or table has no rows — safe to skip


def init():
    import os
    os.makedirs("data", exist_ok=True)

    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL;"))
        conn.execute(text("PRAGMA foreign_keys=ON;"))
        conn.executescript = lambda sql: [conn.execute(text(s.strip())) for s in sql.split(";") if s.strip()]
        for statement in SCHEMA_SQL.split(";"):
            stmt = statement.strip()
            if stmt:
                conn.execute(text(stmt))
        for statement in SEED_SQL.split(";"):
            stmt = statement.strip()
            if stmt:
                conn.execute(text(stmt))
        conn.commit()

    # Migrations — run in separate transactions so each is independent
    _run_migrations()

    print("Server database initialized at data/portfolio_server.db")


if __name__ == "__main__":
    init()
