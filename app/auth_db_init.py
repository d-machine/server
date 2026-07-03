"""
Initialize the auth SQLite database schema (data/auth.db).

Called at server startup alongside db_init.py.
Run standalone: python -m app.auth_db_init
"""

from pathlib import Path
from app.auth_db import auth_engine, AUTH_DB_PATH
from sqlalchemy import text

SCHEMA_SQL = [

"""CREATE TABLE IF NOT EXISTS users (
    user_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    name          TEXT    NOT NULL,
    password_hash TEXT    NOT NULL,
    is_active     INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
)""",

"""CREATE TABLE IF NOT EXISTS refresh_tokens (
    token_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(user_id),
    token_hash   TEXT    NOT NULL UNIQUE,
    issued_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    expires_at   TEXT    NOT NULL,
    revoked      INTEGER NOT NULL DEFAULT 0,
    replaced_by  INTEGER REFERENCES refresh_tokens(token_id)
)""",

"""CREATE TABLE IF NOT EXISTS subscriptions (
    subscription_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL REFERENCES users(user_id),
    plan             TEXT    NOT NULL,
    status           TEXT    NOT NULL DEFAULT 'PENDING_APPROVAL',
    starts_at        TEXT,
    expires_at       TEXT,
    screenshot_path  TEXT,
    cancel_at        TEXT,
    decline_reason   TEXT,
    submitted_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    created_at       TEXT    NOT NULL DEFAULT (datetime('now'))
)""",

"""CREATE TABLE IF NOT EXISTS password_reset_tokens (
    token_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(user_id),
    token_hash  TEXT    NOT NULL UNIQUE,
    expires_at  TEXT    NOT NULL,
    used        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
)""",

]

INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)",
    "CREATE INDEX IF NOT EXISTS idx_refresh_tokens_user ON refresh_tokens(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_refresh_tokens_hash ON refresh_tokens(token_hash)",
    "CREATE INDEX IF NOT EXISTS idx_subscriptions_user ON subscriptions(user_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_hash ON password_reset_tokens(token_hash)",
]


def init():
    Path(AUTH_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with auth_engine.begin() as conn:
        for stmt in SCHEMA_SQL:
            conn.execute(text(stmt))
        for stmt in INDEX_SQL:
            conn.execute(text(stmt))


if __name__ == "__main__":
    init()
    print(f"Auth DB initialized at {AUTH_DB_PATH}")
