import os
from contextlib import contextmanager
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from arthdesk_db import Database, create_engine_for, get_db as _get_db

DB_PATH = os.getenv("DB_PATH", "data/portfolio_server.db")
DATABASE_URL = f"sqlite:///{DB_PATH}"

# One engine, shared with db_init.py (which imports `engine` directly to run
# schema DDL) — built via arthdesk_db so the WAL/PRAGMA setup lives in one
# place instead of being hand-copied here.
engine = create_engine_for(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    """Yields an arthdesk_db.Database, not a raw Session — existing text()-based
    call sites keep working (Database.execute/fetch_one/fetch_all still accept
    an optional params dict); new code should build real Core statements."""
    yield from _get_db(SessionLocal)


@contextmanager
def db_session():
    """Database access for code outside FastAPI's request lifecycle
    (background threads like the bhavcopy sync jobs, standalone scripts) —
    the same arthdesk_db.Database wrapper every router gets via
    Depends(get_db), just constructed directly instead of via DI. Database
    has no close() of its own (matching arthdesk-db's own test convention) —
    the caller here holds the session reference and closes it directly."""
    session = SessionLocal()
    try:
        yield Database(session)
    finally:
        session.close()
