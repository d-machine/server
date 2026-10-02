"""
End-to-end tests for the equity/derivatives bhavcopy sync jobs — this is
genuinely untested territory before Phase 4 (no prior test file existed for
app/cron/bhavcopy/sync/*), and exactly where the two real bugs this phase
fixes (primary_exchange_id/source, instrument_type-vs-contract_type) would
have surfaced on first real invocation. Bypasses GCS/file download — drives
_process_file/_process_chunk directly against synthetic DataFrames, against
an arthdesk_db.Database wrapping a session bound to an in-memory test
engine — the same object every sync job gets from db_session() in
production, just constructed directly instead of via the context manager
(so each test controls its own commit boundary rather than per-file).
"""
import pandas as pd
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from arthdesk_db import Database
from arthdesk_instruments import init_schema, instrument_equity, instrument_derivatives, equity_eod, fo_eod

from app.cron.bhavcopy.sync import nse_eq, bse_eq, nse_fo, bse_fo


@pytest.fixture
def sync_engine():
    """Fresh in-memory db with the full arthdesk_instruments schema."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    init_schema(engine)
    return engine


@pytest.fixture
def db(sync_engine):
    """An arthdesk_db.Database wrapping a session bound to sync_engine —
    exactly what app.database.db_session() hands every sync job in
    production. Tests commit it themselves where they need rows visible to
    a later read through a plain engine.connect()."""
    SessionLocal = sessionmaker(bind=sync_engine)
    session = SessionLocal()
    try:
        yield Database(session)
    finally:
        session.close()


def _equity_rows(engine):
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(select(instrument_equity)).mappings()]


def _equity_eod_rows(engine):
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(select(equity_eod)).mappings()]


def _derivatives_rows(engine):
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(select(instrument_derivatives)).mappings()]


def _fo_eod_rows(engine):
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(select(fo_eod)).mappings()]


class TestNseEquitySync:
    def test_creates_instrument_and_eod_row_with_real_rupees(self, sync_engine, db, monkeypatch):
        df = pd.DataFrame([{
            "ISIN": "INE002A01018", "TckrSymb": "RELIANCE", "FinInstrmNm": "Reliance Industries",
            "FinInstrmId": "300", "FaceVal": "10", "SctySrs": "EQ",
            "OpnPric": "2500.00", "HghPric": "2550.00", "LwPric": "2490.00", "ClsPric": "2530.50",
            "LastPric": "2530.00", "PrvsClsgPric": "2510.00", "SttlmPric": "2530.50",
            "TtlTradgVol": "1000000", "TtlTrfVal": "2530500000", "TtlNbOfTxsExctd": "50000",
            "TradDt": "2026-09-25",
        }])
        monkeypatch.setattr(nse_eq, "load_file_df", lambda *a, **k: df)

        rows_synced = nse_eq._process_file(db, "fake.csv", "2026-09-25")
        db.commit()

        assert rows_synced == 1
        equity_rows = _equity_rows(sync_engine)
        assert len(equity_rows) == 1
        assert equity_rows[0]["isin"] == "INE002A01018"
        assert equity_rows[0]["nse_sym"] == "RELIANCE"
        assert equity_rows[0]["nse_id"] == 300
        assert equity_rows[0]["face_value"] == 10.0

        eod_rows = _equity_eod_rows(sync_engine)
        assert len(eod_rows) == 1
        assert eod_rows[0]["exchange"] == "NSE"
        assert eod_rows[0]["series"] == "EQ"
        assert eod_rows[0]["close_price"] == 2530.50
        assert isinstance(eod_rows[0]["close_price"], float)  # REAL rupees, not paise int

    def test_second_sync_updates_existing_instrument_not_duplicate(self, sync_engine, db, monkeypatch):
        df1 = pd.DataFrame([{
            "ISIN": "INE002A01018", "TckrSymb": "RELIANCE", "FinInstrmNm": "Reliance Industries",
            "ClsPric": "2500.00", "TradDt": "2026-09-25",
        }])
        monkeypatch.setattr(nse_eq, "load_file_df", lambda *a, **k: df1)
        nse_eq._process_file(db, "day1.csv", "2026-09-25")
        db.commit()

        df2 = pd.DataFrame([{
            "ISIN": "INE002A01018", "TckrSymb": "RELIANCE", "FinInstrmNm": "Reliance Industries",
            "FinInstrmId": "300", "ClsPric": "2550.00", "TradDt": "2026-09-26",
        }])
        monkeypatch.setattr(nse_eq, "load_file_df", lambda *a, **k: df2)
        nse_eq._process_file(db, "day2.csv", "2026-09-26")
        db.commit()

        equity_rows = _equity_rows(sync_engine)
        assert len(equity_rows) == 1  # not duplicated
        assert equity_rows[0]["nse_id"] == 300  # backfilled on day 2

        eod_rows = _equity_eod_rows(sync_engine)
        assert len(eod_rows) == 2  # one EOD row per day


class TestBseEquitySync:
    def test_bse_fin_instr_id_maps_to_bse_id_not_bse_sym(self, sync_engine, db, monkeypatch):
        """Regression for the real bug found while reading this file: BSE's
        FinInstrmId (a numeric scrip code) used to be stuffed into a
        generically-named field and BSE's own ticker was stored under an
        NSE-named key. Must now map correctly: FinInstrmId -> bse_id,
        TckrSymb -> bse_sym."""
        df = pd.DataFrame([{
            "ISIN": "INE917I01010", "TckrSymb": "BAJAJ-AUTO", "FinInstrmNm": "BAJAJ AUTO LTD.",
            "FinInstrmId": "532977", "ClsPric": "11300.00", "SctySrs": "A", "TradDt": "2026-09-25",
        }])
        monkeypatch.setattr(bse_eq, "load_file_df", lambda *a, **k: df)

        bse_eq._process_file(db, "fake.csv", "2026-09-25")
        db.commit()

        rows = _equity_rows(sync_engine)
        assert len(rows) == 1
        assert rows[0]["bse_id"] == 532977
        assert rows[0]["bse_sym"] == "BAJAJ-AUTO"
        assert rows[0]["nse_sym"] is None  # must NOT leak into the NSE field


class TestFoSyncNseAndBseStaySeparate:
    def test_nse_and_bse_same_fin_id_create_two_separate_contracts(self, sync_engine, db, monkeypatch):
        """The core Phase 4 design change, exercised through the real sync
        jobs (not just the DAO layer directly): NSE and BSE F&O contracts
        with the *same* FinInstrmId number must NOT be merged into one row
        — they're independent contracts on independent exchanges."""
        # Seed the underlying equity + index so resolve_underlying can find it.
        eq_df = pd.DataFrame([{
            "ISIN": "INE002A01018", "TckrSymb": "RELIANCE", "FinInstrmNm": "Reliance Industries",
            "ClsPric": "2500.00", "TradDt": "2026-09-25",
        }])
        monkeypatch.setattr(nse_eq, "load_file_df", lambda *a, **k: eq_df)
        nse_eq._process_file(db, "eq.csv", "2026-09-25")
        db.commit()

        fo_row = {
            "FinInstrmId": "7001", "TckrSymb": "RELIANCE", "FinInstrmTp": "STF",
            "XpryDt": "2026-09-25", "StrkPric": "0", "OptnTp": "",
            "ClsPric": "2510.00", "TradDt": "2026-09-25",
        }
        nse_df = pd.DataFrame([fo_row])
        monkeypatch.setattr(nse_fo, "load_file_chunks", lambda *a, **k: [nse_df])
        nse_fo._process_file(db, "nse_fo.csv", "2026-09-25")
        db.commit()

        bse_df = pd.DataFrame([fo_row])
        monkeypatch.setattr(bse_fo, "load_file_df", lambda *a, **k: bse_df)
        bse_fo._process_file(db, "bse_fo.csv", "2026-09-25")
        db.commit()

        derivative_rows = _derivatives_rows(sync_engine)
        assert len(derivative_rows) == 2
        exchanges = {r["exchange"] for r in derivative_rows}
        assert exchanges == {"NSE", "BSE"}
        ids = {r["instr_id"] for r in derivative_rows}
        assert len(ids) == 2  # genuinely two different instruments

        fo_eod_rows = _fo_eod_rows(sync_engine)
        assert len(fo_eod_rows) == 2

    def test_contract_type_column_is_written_correctly(self, sync_engine, db, monkeypatch):
        """Regression: bulk_create_fo used to write a column named
        instrument_type that doesn't exist in the schema (real column is
        contract_type) — would have raised OperationalError at runtime."""
        eq_df = pd.DataFrame([{"ISIN": "INE002A01018", "TckrSymb": "RELIANCE", "ClsPric": "2500.00"}])
        monkeypatch.setattr(nse_eq, "load_file_df", lambda *a, **k: eq_df)
        nse_eq._process_file(db, "eq.csv", "2026-09-25")
        db.commit()

        fo_df = pd.DataFrame([{
            "FinInstrmId": "8001", "TckrSymb": "RELIANCE", "FinInstrmTp": "STF",
            "XpryDt": "2026-09-25", "StrkPric": "0", "OptnTp": "",
            "ClsPric": "2510.00", "TradDt": "2026-09-25",
        }])
        monkeypatch.setattr(nse_fo, "load_file_chunks", lambda *a, **k: [fo_df])

        nse_fo._process_file(db, "fo.csv", "2026-09-25")  # must not raise
        db.commit()

        rows = _derivatives_rows(sync_engine)
        assert len(rows) == 1
        assert rows[0]["contract_type"] == "STF"
