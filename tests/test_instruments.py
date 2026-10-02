"""Tests for /instruments/* endpoints."""

from unittest.mock import patch
import pytest

from tests.conftest import seed_equity, seed_index, seed_derivative


# ── Unprotected endpoints ─────────────────────────────────────────────────────

class TestInstrumentTypes:
    def test_types_no_auth_required(self, client):
        r = client.get("/instruments/types")
        assert r.status_code == 200
        data = r.json()
        types = data if isinstance(data, list) else data.get("instrument_types", data)
        assert any(t["name"] == "EQUITY" for t in types)

    def test_types_use_canonical_asset_class_codes(self, client):
        """asset_class values must match arthdesk-py's AssetClass enum, not the old
        server-internal 'MF'/'DERIVATIVE' abbreviations."""
        r = client.get("/instruments/types")
        types = r.json()["instrument_types"]
        codes = {t["asset_class"] for t in types}
        assert "MUTUAL_FUND" in codes
        assert "DERIVATIVES" in codes
        assert "MF" not in codes
        assert "DERIVATIVE" not in codes


class TestAssetClasses:
    def test_asset_classes_no_auth_required(self, client):
        r = client.get("/instruments/asset-classes")
        assert r.status_code == 200
        codes = {a["code"] for a in r.json()["asset_classes"]}
        assert codes == {"EQUITY", "INDEX", "MUTUAL_FUND", "FIXED_INCOME", "DERIVATIVES", "COMMODITY", "PENDING"}

    def test_get_by_isin_no_auth(self, client, main_engine):
        iid = seed_equity(main_engine, isin="INE000000001", symbol="TESTSYM")
        r = client.get("/instruments/INE000000001")
        assert r.status_code == 200
        assert r.json()["isin"] == "INE000000001"

    def test_get_by_isin_not_found(self, client):
        r = client.get("/instruments/INE999999999")
        assert r.status_code == 404


# ── Protected endpoints (require active subscription) ─────────────────────────

class TestInstrumentUpdates:
    def test_updates_requires_auth(self, client):
        r = client.get("/instruments/updates")
        assert r.status_code in (401, 422)

    def test_updates_requires_subscription(self, client, bearer):
        r = client.get("/instruments/updates", headers=bearer)
        assert r.status_code == 403
        assert r.json()["detail"] == "subscription_required"

    def test_updates_with_subscription(self, client, bearer_with_sub):
        r = client.get("/instruments/updates")
        # Without auth should fail
        assert r.status_code in (401, 422)

    def test_updates_with_valid_sub(self, client, bearer_with_sub, main_engine):
        r = client.get("/instruments/updates", headers=bearer_with_sub)
        # May return empty list, but not 401/403
        assert r.status_code == 200


class TestInstrumentResolve:
    def test_resolve_requires_subscription(self, client, bearer):
        r = client.post("/instruments/resolve", json={"isins": ["INE000000001"]}, headers=bearer)
        assert r.status_code == 403

    def test_resolve_with_subscription(self, client, bearer_with_sub, main_engine):
        iid = seed_equity(main_engine, isin="INE111111111", symbol="AAA")
        r = client.post("/instruments/resolve", json=[{
            "pending_id": 1, "instrument_type": "EQUITY", "isin": "INE111111111"
        }], headers=bearer_with_sub)
        assert r.status_code == 200

    def test_resolve_unknown_isin_returns_empty(self, client, bearer_with_sub):
        r = client.post("/instruments/resolve", json=[{
            "pending_id": 1, "instrument_type": "EQUITY", "isin": "INE999999999"
        }], headers=bearer_with_sub)
        assert r.status_code == 200
        body = r.json()
        resolved = body if isinstance(body, list) else body.get("resolved", [])
        assert resolved == []


class TestInstrumentSearch:
    def test_search_requires_subscription(self, client, bearer):
        r = client.get("/instruments/search?q=test", headers=bearer)
        assert r.status_code == 403

    def test_search_with_subscription(self, client, bearer_with_sub, main_engine):
        seed_equity(main_engine, isin="INE222222222", symbol="SEARCHME", name="SearchTest Corp")
        r = client.get("/instruments/search?q=SearchTest", headers=bearer_with_sub)
        assert r.status_code == 200
        body = r.json()
        results = body if isinstance(body, list) else body.get("results", [])
        assert isinstance(results, list)

    def test_search_mutual_fund_asset_class_filter(self, client, bearer_with_sub, main_engine):
        """Regression: asset_class='MUTUAL_FUND' must still match rows whose
        instrument_types.asset_class is 'MUTUAL_FUND' (post-rename from 'MF')."""
        from sqlalchemy import text
        with main_engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO instruments (instrument_id, name, instrument_type_id, is_active, created_at, updated_at)
                SELECT 9001, 'SearchTest Fund', instrument_type_id, 1, datetime('now'), datetime('now')
                FROM instrument_types WHERE name = 'EQUITY_MF'
            """))
            conn.execute(text("""
                INSERT INTO instrument_mf (instrument_id, isin, amfi_code, fund_house)
                VALUES (9001, 'INF999999999', '999999', 'Test AMC')
            """))
        r = client.get("/instruments/search?q=SearchTest&asset_class=MUTUAL_FUND", headers=bearer_with_sub)
        assert r.status_code == 200
        results = r.json()["results"]
        assert any(row["instrument_id"] == 9001 for row in results)

    def test_search_no_results(self, client, bearer_with_sub):
        r = client.get("/instruments/search?q=ZZZNOMATCHZZZ", headers=bearer_with_sub)
        assert r.status_code == 200
        body = r.json()
        results = body if isinstance(body, list) else body.get("results", [])
        assert results == []

    def test_search_requires_query_param(self, client, bearer_with_sub):
        r = client.get("/instruments/search", headers=bearer_with_sub)
        assert r.status_code == 422


# ── Equity create endpoint ────────────────────────────────────────────────────

class TestEquityCreate:
    def test_create_equity_endpoint_exists(self, client, main_engine):
        """POST /instruments/equity: if instrument is new, endpoint returns its data.
        The create path uses app.database.engine directly (bypasses test override),
        so we only test the 'already exists' path via seed_equity."""
        seed_equity(main_engine, isin="INE333333333", symbol="NEWSYM", name="New Corp")
        r = client.post("/instruments/equity", json={
            "isin": "INE333333333",
            "nse_symbol": "NEWSYM",
            "name": "New Corp",
        })
        # Existing instrument is returned via the test DB (dependency override path)
        assert r.status_code == 200
        assert r.json()["isin"] == "INE333333333"
        assert r.json()["created"] is False

    def test_create_equity_persists_and_is_idempotent(self, client):
        """Creating a brand-new instrument writes through the injected db
        session (fixed this round — previously bypassed it entirely via a
        direct app.database.engine connection, per the old test's own
        comment). Must actually commit and be visible to a later request,
        and calling it again with the same isin must not duplicate."""
        r1 = client.post("/instruments/equity", json={
            "isin": "INE777777777",
            "nse_symbol": "NEWONE",
            "nse_fininstrmid": 555,
            "name": "Brand New Corp",
            "face_value_paise": 1000,
        })
        assert r1.status_code == 200
        body1 = r1.json()
        assert body1["created"] is True
        assert body1["isin"] == "INE777777777"
        assert body1["nse_symbol"] == "NEWONE"
        assert body1["nse_fininstrmid"] == 555
        assert body1["face_value_paise"] == 1000
        assert body1["sector"] is None  # dropped from the schema in Phase 2

        # A later request (separate session, same underlying db) must see it.
        r2 = client.get("/instruments/INE777777777")
        assert r2.status_code == 200
        assert r2.json()["isin"] == "INE777777777"

        # Calling create again with the same isin must not duplicate.
        r3 = client.post("/instruments/equity", json={
            "isin": "INE777777777",
            "nse_symbol": "NEWONE",
            "name": "Brand New Corp",
        })
        assert r3.status_code == 200
        assert r3.json()["created"] is False
        assert r3.json()["instrument_id"] == body1["instrument_id"]

    def test_create_equity_fixes_the_primary_exchange_id_bug(self, client):
        """Regression: the pre-Phase-4 endpoint inserted primary_exchange_id/
        source columns that didn't exist in the schema and would raise at
        runtime. Since the new path writes only real Core-defined columns,
        this can no longer reproduce."""
        r = client.post("/instruments/equity", json={
            "isin": "INE888888888",
            "name": "No Crash Corp",
        })
        assert r.status_code == 200


# ── /resolve: index + derivatives (new this round) ────────────────────────────

class TestIndexAndDerivativesResolve:
    def test_resolve_index_by_symbol(self, client, bearer_with_sub, main_engine):
        seed_index(main_engine, sym="BANKNIFTY")
        r = client.post("/instruments/resolve", json=[{
            "pending_id": 1, "instrument_type": "INDEX", "nse_symbol": "BANKNIFTY",
        }], headers=bearer_with_sub)
        assert r.status_code == 200
        resolved = r.json()["resolved"]
        assert len(resolved) == 1
        assert resolved[0]["index_symbol"] == "BANKNIFTY"

    def test_resolve_futures_by_exchange_and_fin_id(self, client, bearer_with_sub, main_engine):
        seed_derivative(main_engine, exchange="NSE", nse_bse_id=2001)
        r = client.post("/instruments/resolve", json=[{
            "pending_id": 1, "instrument_type": "FUTURES",
            "exchange": "NSE", "nse_fininstrmid": 2001,
        }], headers=bearer_with_sub)
        assert r.status_code == 200
        resolved = r.json()["resolved"]
        assert len(resolved) == 1
        assert resolved[0]["fo_nse_fininstrmid"] == 2001
        assert resolved[0]["fo_bse_fininstrmid"] is None

    def test_nse_and_bse_contracts_with_same_fin_id_do_not_collide(self, client, bearer_with_sub, main_engine):
        """The core Phase 4 design change: NSE and BSE F&O contracts are
        separate rows (exchange discriminator), not merged via two id
        columns on one row — so the same numeric id on each exchange must
        resolve to two different instruments."""
        nse_id = seed_derivative(main_engine, exchange="NSE", nse_bse_id=3001)
        bse_id = seed_derivative(main_engine, exchange="BSE", nse_bse_id=3001)
        assert nse_id != bse_id

        r_nse = client.post("/instruments/resolve", json=[{
            "pending_id": 1, "instrument_type": "FUTURES", "exchange": "NSE", "nse_fininstrmid": 3001,
        }], headers=bearer_with_sub)
        r_bse = client.post("/instruments/resolve", json=[{
            "pending_id": 2, "instrument_type": "FUTURES", "exchange": "BSE", "nse_fininstrmid": 3001,
        }], headers=bearer_with_sub)

        assert r_nse.json()["resolved"][0]["instrument_id"] == nse_id
        assert r_bse.json()["resolved"][0]["instrument_id"] == bse_id

    def test_resolve_unknown_derivative_returns_empty(self, client, bearer_with_sub):
        r = client.post("/instruments/resolve", json=[{
            "pending_id": 1, "instrument_type": "FUTURES", "exchange": "NSE", "nse_fininstrmid": 999999,
        }], headers=bearer_with_sub)
        assert r.status_code == 200
        assert r.json()["resolved"] == []

    def test_mixed_batch_with_unmigrated_mf_ref_does_not_fail_other_refs(self, client, bearer_with_sub, main_engine):
        """Containment: an MF ref hitting the not-yet-migrated schema must
        not take down resolution of other (migrated) refs in the same batch."""
        seed_equity(main_engine, isin="INE444444444", symbol="SAFE")
        r = client.post("/instruments/resolve", json=[
            {"pending_id": 1, "instrument_type": "EQUITY", "isin": "INE444444444"},
            {"pending_id": 2, "instrument_type": "EQUITY_MF", "amfi_code": "123456"},
        ], headers=bearer_with_sub)
        assert r.status_code == 200
        resolved = r.json()["resolved"]
        assert len(resolved) == 1
        assert resolved[0]["isin"] == "INE444444444"
