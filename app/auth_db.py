import os
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

AUTH_DB_PATH = os.getenv("AUTH_DB_PATH", "data/auth.db")
AUTH_DATABASE_URL = f"sqlite:///{AUTH_DB_PATH}"

auth_engine = create_engine(AUTH_DATABASE_URL, connect_args={"check_same_thread": False})


@event.listens_for(auth_engine, "connect")
def on_connect(conn, _):
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA synchronous=NORMAL;")


AuthSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=auth_engine)


def get_auth_db():
    db = AuthSessionLocal()
    try:
        yield db
    finally:
        db.close()
