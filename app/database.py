import os
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DB_PATH = os.getenv("DB_PATH", "data/portfolio_server.db")
DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})


# Enable WAL mode and tune SQLite on every new connection
@event.listens_for(engine, "connect")
def on_connect(conn, _):
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA cache_size=-64000;")       # 64 MB page cache
    conn.execute("PRAGMA synchronous=NORMAL;")      # safe with WAL, faster than FULL
    conn.execute("PRAGMA wal_autocheckpoint=1000;") # checkpoint every 1000 pages (~4 MB)


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
