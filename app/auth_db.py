import os
from sqlalchemy.orm import sessionmaker

from arthdesk_db import create_engine_for, get_db as _get_db

AUTH_DB_PATH = os.getenv("AUTH_DB_PATH", "data/auth.db")
AUTH_DATABASE_URL = f"sqlite:///{AUTH_DB_PATH}"

# Separate physical database from app/database.py's engine — auth.db vs
# portfolio_server.db. Built via arthdesk_db for the same reason: shared,
# tested WAL/PRAGMA setup instead of hand-copied per engine.
auth_engine = create_engine_for(AUTH_DATABASE_URL)
AuthSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=auth_engine)


def get_auth_db():
    """Yields an arthdesk_db.Database for the auth database."""
    yield from _get_db(AuthSessionLocal)
