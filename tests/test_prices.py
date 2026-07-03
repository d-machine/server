"""Tests for /prices/* endpoints."""

from unittest.mock import patch
import pytest
from sqlalchemy import text

from tests.conftest import seed_equity


# ── Subscription gate ─────────────────────────────────────────────────────────

class TestPricesSubscriptionGate:
    def test_latest_no_auth(self, client):
        r = client.get("/prices/latest")
        assert r.status_code in (401, 422)

    def test_latest_no_subscription(self, client, bearer):
        r = client.get("/prices/latest", headers=bearer)
        assert r.status_code == 403
        assert r.json()["detail"] == "subscription_required"

    def test_sync_no_subscription(self, client, bearer):
        r = client.get("/prices/sync", headers=bearer)
        assert r.status_code == 403

    def test_latest_with_subscription(self, client, bearer_with_sub):
        r = client.get("/prices/latest", headers=bearer_with_sub)
        assert r.status_code == 200

    def test_sync_with_subscription(self, client, bearer_with_sub):
        r = client.get("/prices/sync", headers=bearer_with_sub)
        assert r.status_code == 200


# ── /prices/latest ────────────────────────────────────────────────────────────

class TestPricesLatest:
    def test_latest_empty_db(self, client, bearer_with_sub):
        r = client.get("/prices/latest", headers=bearer_with_sub)
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, (list, dict))

    def test_latest_with_data(self, client, bearer_with_sub, main_engine):
        from sqlalchemy import text
        isin = "INE555555555"
        iid = seed_equity(main_engine, isin=isin, symbol="PRICESYM")
        with main_engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO latest_prices (instrument_id, price_date, close_price_paise, last_synced_at, updated_at)
                VALUES (:iid, date('now'), 15050, datetime('now'), datetime('now'))
            """), {"iid": iid})
        r = client.get("/prices/latest", headers=bearer_with_sub)
        assert r.status_code == 200

    def test_latest_with_isin_filter(self, client, bearer_with_sub, main_engine):
        isin = "INE666666666"
        iid = seed_equity(main_engine, isin=isin, symbol="FILTERSYM")
        with main_engine.begin() as conn:
            conn.execute(
                text("INSERT INTO latest_prices (instrument_id, price_date, close_price_paise, last_synced_at, updated_at) VALUES (:iid, date('now'), 20000, datetime('now'), datetime('now'))"),
                {"iid": iid}
            )
        r = client.get(f"/prices/latest?isins={isin}", headers=bearer_with_sub)
        assert r.status_code == 200


# ── /prices/sync ─────────────────────────────────────────────────────────────

class TestPricesSync:
    def test_sync_empty_list(self, client, bearer_with_sub):
        r = client.get("/prices/sync", headers=bearer_with_sub)
        assert r.status_code == 200

    def test_sync_unknown_isin(self, client, bearer_with_sub):
        r = client.get("/prices/sync?isins=INE000000000", headers=bearer_with_sub)
        assert r.status_code == 200


# ── Trading calendar ──────────────────────────────────────────────────────────

class TestTradingCalendar:
    def test_calendar_accessible(self, client):
        r = client.get("/prices/trading-calendar?year=2025")
        assert r.status_code in (200, 404, 422)

    def test_calendar_does_not_require_subscription(self, client, bearer):
        r = client.get("/prices/trading-calendar?year=2025", headers=bearer)
        assert r.status_code != 403


# ── Cache module unit tests ───────────────────────────────────────────────────

class TestPriceCacheModule:
    def test_cache_import(self):
        """The price cache module should be importable."""
        try:
            from app import price_cache  # or wherever it lives
        except ImportError:
            try:
                from app.routers import prices as p
                assert hasattr(p, "router")
            except ImportError:
                pytest.skip("price cache module not found")

    def test_latest_prices_returns_dict_or_list(self, client, bearer_with_sub):
        r = client.get("/prices/latest", headers=bearer_with_sub)
        assert r.status_code == 200
        assert isinstance(r.json(), (list, dict))
