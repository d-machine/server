"""
Shared fixtures for the server test suite.

Each test gets:
- A fresh in-memory SQLite for both the market-data DB and the auth DB
- A FastAPI TestClient wired to those in-memory DBs
- Convenience helpers: registered user, valid tokens, active subscription
"""

import base64
import hashlib
import os
import secrets
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# ── Patch env vars BEFORE importing app modules ──────────────────────────────
os.environ.setdefault("JWT_SECRET", "test-secret-key-32-bytes-exactly!!")
os.environ.setdefault("ADMIN_USER", "admin")
os.environ.setdefault("ADMIN_PASS", "adminpass")
os.environ.setdefault("SMTP_PASS", "")   # disable email sending in tests
os.environ.setdefault("BASE_URL", "http://testserver")

from app.auth_db_init import SCHEMA_SQL as AUTH_SCHEMA, INDEX_SQL as AUTH_INDEX
from app.db_init import SCHEMA_SQL as MAIN_SCHEMA, INDEX_SQL as MAIN_INDEX, SEED_SQL


# ── In-memory DB factories ────────────────────────────────────────────────────

def _make_engine(schema_sqls, index_sqls, seed_sqls=None):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,  # share ONE connection so in-memory DB persists
    )

    @event.listens_for(engine, "connect")
    def on_connect(conn, _):
        conn.execute("PRAGMA foreign_keys=ON;")

    with engine.begin() as conn:
        for stmt in schema_sqls:
            conn.execute(text(stmt))
        for stmt in index_sqls:
            conn.execute(text(stmt))
        if seed_sqls:
            for stmt in seed_sqls:
                conn.execute(text(stmt))
    return engine


@pytest.fixture(scope="function")
def main_engine():
    return _make_engine(MAIN_SCHEMA, MAIN_INDEX, SEED_SQL)


@pytest.fixture(scope="function")
def auth_engine():
    return _make_engine(AUTH_SCHEMA, AUTH_INDEX)


@pytest.fixture(scope="function")
def client(main_engine, auth_engine):
    """TestClient with both DBs patched to in-memory engines."""
    from app import main as app_module
    from app import database as db_module
    from app import auth_db as auth_db_module

    main_session = sessionmaker(autocommit=False, autoflush=False, bind=main_engine)
    auth_session = sessionmaker(autocommit=False, autoflush=False, bind=auth_engine)

    def override_get_db():
        db = main_session()
        try:
            yield db
        finally:
            db.close()

    def override_get_auth_db():
        db = auth_session()
        try:
            yield db
        finally:
            db.close()

    from app import database, auth_db

    app_module.app.dependency_overrides[database.get_db]      = override_get_db
    app_module.app.dependency_overrides[auth_db.get_auth_db]  = override_get_auth_db

    # Patch GCS and email sending globally for all tests
    with patch("app.routers.subscriptions._gcs_bucket"), \
         patch("app.routers.subscriptions._send_email"), \
         patch("app.routers.auth._send_email"), \
         patch("app.routers.deps._send_underpaid_email"):
        with TestClient(app_module.app, raise_server_exceptions=True) as c:
            yield c

    app_module.app.dependency_overrides.clear()


# ── Auth helpers ──────────────────────────────────────────────────────────────

def register_user(client, email="test@example.com", name="Test User", password="password123"):
    r = client.post("/auth/register", json={"email": email, "name": name, "password": password})
    assert r.status_code == 201, r.text
    return r.json()


def login_user(client, email="test@example.com", password="password123"):
    r = client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def auth_headers(client, email="test@example.com", password="password123"):
    data = login_user(client, email, password)
    return {"Authorization": f"Bearer {data['access_token']}"}


@pytest.fixture
def registered_user(client):
    register_user(client)
    return login_user(client)


@pytest.fixture
def bearer(registered_user):
    return {"Authorization": f"Bearer {registered_user['access_token']}"}


@pytest.fixture
def admin_headers():
    creds = base64.b64encode(b"admin:adminpass").decode()
    return {"Authorization": f"Basic {creds}"}


def create_person(client, bearer, pan_hash="aabbcc", masked_pan="ABCDE****F", display_name="Test Person"):
    r = client.post(
        "/persons",
        json={"pan_hash": pan_hash, "masked_pan": masked_pan, "display_name": display_name},
        headers=bearer,
    )
    assert r.status_code == 201, r.text
    return r.json()["person_id"]


@pytest.fixture
def person_id(client, bearer):
    return create_person(client, bearer)


@pytest.fixture
def active_subscription(client, registered_user, admin_headers, bearer, person_id):
    """Give the test person an ACTIVE subscription via the admin approve endpoint."""
    from io import BytesIO
    import json
    persons_payload = json.dumps([{"person_id": person_id, "amount": 1000}])
    with patch("app.routers.subscriptions._gcs_bucket"):
        r = client.post(
            "/subscriptions/submit",
            data={"persons": persons_payload},
            files={"screenshot": ("test.png", BytesIO(b"fake"), "image/png")},
            headers=bearer,
        )
    assert r.status_code == 200, r.text
    sub_id = r.json()["created"][0]["subscription_id"]

    r = client.post(f"/subscriptions/admin/{sub_id}/approve", headers=admin_headers)
    assert r.status_code == 200, r.text
    return sub_id


@pytest.fixture
def bearer_with_sub(registered_user, active_subscription):
    return {"Authorization": f"Bearer {registered_user['access_token']}"}


# ── Instrument seed helper ────────────────────────────────────────────────────

def seed_equity(main_engine, isin="INE123456789", symbol="TESTSYM", name="Test Corp"):
    """Insert a minimal equity instrument directly into the test DB."""
    with main_engine.begin() as conn:
        # Get EQUITY type id
        row = conn.execute(
            text("SELECT instrument_type_id FROM instrument_types WHERE name='EQUITY'")
        ).fetchone()
        type_id = row[0]

        conn.execute(text("""
            INSERT INTO instruments (name, instrument_type_id, is_active)
            VALUES (:name, :tid, 1)
        """), {"name": name, "tid": type_id})

        iid = conn.execute(text("SELECT last_insert_rowid()")).scalar()

        conn.execute(text("""
            INSERT INTO instrument_equity (instrument_id, isin, nse_symbol)
            VALUES (:iid, :isin, :sym)
        """), {"iid": iid, "isin": isin, "sym": symbol})

        return iid
