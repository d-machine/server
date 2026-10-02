"""Tests for /prices/* endpoints."""

from sqlalchemy import text

from tests.conftest import seed_equity


def _seed_latest_price(main_engine, instr_id, price=2530.5, exchange="NSE",
                        price_date="2026-09-25"):
    with main_engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO latest_prices (instr_id, exchange, price_date, price, last_synced_at)
            VALUES (:iid, :exch, :pd, :price, datetime('now'))
        """), {"iid": instr_id, "exch": exchange, "pd": price_date, "price": price})


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
        assert data["prices"] == {}


# ── /prices/sync ─────────────────────────────────────────────────────────────

class TestPricesSync:
    def test_sync_empty_list(self, client, bearer_with_sub):
        r = client.get("/prices/sync", headers=bearer_with_sub)
        assert r.status_code == 200
        assert r.json()["prices"] == []

    def test_sync_unknown_instrument_id(self, client, bearer_with_sub):
        r = client.get("/prices/sync?instrument_ids=999999", headers=bearer_with_sub)
        assert r.status_code == 200
        assert r.json()["prices"] == []

    def test_sync_returns_real_price_for_known_instrument(self, client, bearer_with_sub, main_engine):
        iid = seed_equity(main_engine, isin="INE555555555", symbol="PRICESYM")
        _seed_latest_price(main_engine, iid, price=2530.5)

        r = client.get(f"/prices/sync?instrument_ids={iid}", headers=bearer_with_sub)
        assert r.status_code == 200
        data = r.json()
        assert len(data["prices"]) == 1
        row = data["prices"][0]
        assert row["instrument_id"] == iid
        assert row["exchange"] == "NSE"
        assert row["price"] == 2530.5
        assert isinstance(row["price"], float)
        assert data["synced_at"] is not None

    def test_sync_since_datetime_excludes_unchanged_rows(self, client, bearer_with_sub, main_engine):
        iid = seed_equity(main_engine, isin="INE666666666", symbol="FILTERSYM")
        _seed_latest_price(main_engine, iid, price=2000.0)

        far_future = "9999-01-01T00:00:00"
        r = client.get(
            f"/prices/sync?instrument_ids={iid}&since_datetime={far_future}",
            headers=bearer_with_sub,
        )
        assert r.status_code == 200
        assert r.json()["prices"] == []


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
        from app import cache
        assert hasattr(cache, "warm_cache")

    def test_latest_prices_returns_dict(self, client, bearer_with_sub):
        r = client.get("/prices/latest", headers=bearer_with_sub)
        assert r.status_code == 200
        assert isinstance(r.json()["prices"], dict)
