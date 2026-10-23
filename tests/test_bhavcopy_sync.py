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
import threading

import pandas as pd
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from arthdesk_db import Database
from arthdesk_instruments import (
    init_schema, instrument_equity, instrument_derivatives, equity_eod, fo_eod, latest_prices,
)

from app.cron.bhavcopy.sync import nse_eq, bse_eq, nse_fo, bse_fo
from app.cron.bhavcopy.sync.base import get_pending_files, instrument_write_lock
from app.cron.bhavcopy.constants import FileStatus
from app.db_init import SCHEMA_SQL as SERVER_SCHEMA_SQL


@pytest.fixture
def sync_engine():
    """Fresh in-memory db with the full arthdesk_instruments schema plus the
    server's own operational tables (bhavcopy_files etc.), needed by the
    get_pending_files tests below."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,  # one shared connection, so threads see the same db
    )
    init_schema(engine)
    with engine.begin() as conn:
        for stmt in SERVER_SCHEMA_SQL:
            conn.execute(text(stmt))
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


def _latest_price_rows(engine):
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(select(latest_prices)).mappings()]


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

        latest_rows = _latest_price_rows(sync_engine)
        assert len(latest_rows) == 1
        assert latest_rows[0]["exchange"] == "NSE"
        assert latest_rows[0]["price"] == 2530.50
        assert latest_rows[0]["last_synced_at"] is not None

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

        latest_rows = _latest_price_rows(sync_engine)
        assert len(latest_rows) == 1  # one cache row, not one per day
        assert latest_rows[0]["price"] == 2550.0  # day 2's close, not day 1's
        assert latest_rows[0]["price_date"] == "2026-09-26"


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

        latest_rows = _latest_price_rows(sync_engine)
        assert len(latest_rows) == 1
        assert latest_rows[0]["exchange"] == "BSE"
        assert latest_rows[0]["price"] == 11300.0


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


class TestFoRecycledFinInstrmId:
    """
    Regression for a real production bug found during the Jan-Oct 2026
    F&O backfill: NSE (and, by the same mechanism, BSE) reuses a
    FinInstrmId for an unrelated contract once the original expires.
    Trusting an id-based match blindly (the old behavior) silently wrote
    the NEW contract's price data onto the OLD contract's row -- e.g. id
    151946 belonged to "TECHM 2026-01-27 1800 CE" (expired), then NSE
    reissued it for "SBIN 2026-05-26 1200 CE": the sync kept treating
    every SBIN row as if it were the TECHM contract, so SBIN's own
    instrument_derivatives row never got created at all.
    """

    def test_nse_recycled_fin_instr_id_creates_new_contract_not_overwrite(
        self, sync_engine, db, monkeypatch
    ):
        eq_df = pd.DataFrame([
            {"ISIN": "INE009A01021", "TckrSymb": "TECHM",  "ClsPric": "1700.00"},
            {"ISIN": "INE062A01020", "TckrSymb": "SBIN",   "ClsPric": "800.00"},
        ])
        monkeypatch.setattr(nse_eq, "load_file_df", lambda *a, **k: eq_df)
        nse_eq._process_file(db, "eq.csv", "2026-01-05")
        db.commit()

        # Day 1: id 151946 is TECHM's 27-Jan-2026 1800 CE option.
        techm_df = pd.DataFrame([{
            "FinInstrmId": "151946", "TckrSymb": "TECHM", "FinInstrmTp": "STO",
            "XpryDt": "2026-01-27", "StrkPric": "1800", "OptnTp": "CE",
            "ClsPric": "12.50", "TradDt": "2026-01-05",
        }])
        monkeypatch.setattr(nse_fo, "load_file_chunks", lambda *a, **k: [techm_df])
        nse_fo._process_file(db, "fo_day1.csv", "2026-01-05")
        db.commit()

        rows = _derivatives_rows(sync_engine)
        assert len(rows) == 1
        techm_instr_id = rows[0]["instr_id"]
        assert rows[0]["expd"] == "2026-01-27"
        assert rows[0]["strkp"] == 1800.0

        # Day 2 (after TECHM's contract has expired): NSE has recycled the
        # SAME id for an unrelated SBIN contract.
        sbin_df = pd.DataFrame([{
            "FinInstrmId": "151946", "TckrSymb": "SBIN", "FinInstrmTp": "STO",
            "XpryDt": "2026-05-26", "StrkPric": "1200", "OptnTp": "CE",
            "ClsPric": "41.75", "TradDt": "2026-05-08",
        }])
        monkeypatch.setattr(nse_fo, "load_file_chunks", lambda *a, **k: [sbin_df])
        nse_fo._process_file(db, "fo_day2.csv", "2026-05-08")
        db.commit()

        rows = _derivatives_rows(sync_engine)
        assert len(rows) == 2, "SBIN's contract must get its own row, not reuse TECHM's"

        techm_row = next(r for r in rows if r["instr_id"] == techm_instr_id)
        assert techm_row["expd"] == "2026-01-27"
        assert techm_row["strkp"] == 1800.0

        sbin_row = next(r for r in rows if r["instr_id"] != techm_instr_id)
        assert sbin_row["expd"] == "2026-05-26"
        assert sbin_row["strkp"] == 1200.0
        assert sbin_row["opn_type"] == "CE"

        # Each contract's own price row is attached to ITS OWN instr_id,
        # not mixed together under the recycled id.
        eod_rows = _fo_eod_rows(sync_engine)
        techm_eod = [r for r in eod_rows if r["instr_id"] == techm_instr_id]
        sbin_eod  = [r for r in eod_rows if r["instr_id"] == sbin_row["instr_id"]]
        assert len(techm_eod) == 1 and techm_eod[0]["close_price"] == 12.50
        assert len(sbin_eod) == 1 and sbin_eod[0]["close_price"] == 41.75

    def test_bse_recycled_fin_instr_id_creates_new_contract_not_overwrite(
        self, sync_engine, db, monkeypatch
    ):
        eq_df = pd.DataFrame([
            {"ISIN": "INE009A01021", "TckrSymb": "TECHM", "ClsPric": "1700.00"},
            {"ISIN": "INE062A01020", "TckrSymb": "SBIN",  "ClsPric": "800.00"},
        ])
        monkeypatch.setattr(nse_eq, "load_file_df", lambda *a, **k: eq_df)
        nse_eq._process_file(db, "eq.csv", "2026-01-05")
        db.commit()

        techm_df = pd.DataFrame([{
            "FinInstrmId": "222333", "TckrSymb": "TECHM", "FinInstrmTp": "STO",
            "XpryDt": "2026-01-27", "StrkPric": "1800", "OptnTp": "CE",
            "ClsPric": "12.50", "TradDt": "2026-01-05",
        }])
        monkeypatch.setattr(bse_fo, "load_file_df", lambda *a, **k: techm_df)
        bse_fo._process_file(db, "fo_day1.csv", "2026-01-05")
        db.commit()

        techm_instr_id = _derivatives_rows(sync_engine)[0]["instr_id"]

        sbin_df = pd.DataFrame([{
            "FinInstrmId": "222333", "TckrSymb": "SBIN", "FinInstrmTp": "STO",
            "XpryDt": "2026-05-26", "StrkPric": "1200", "OptnTp": "CE",
            "ClsPric": "41.75", "TradDt": "2026-05-08",
        }])
        monkeypatch.setattr(bse_fo, "load_file_df", lambda *a, **k: sbin_df)
        bse_fo._process_file(db, "fo_day2.csv", "2026-05-08")
        db.commit()

        rows = _derivatives_rows(sync_engine)
        assert len(rows) == 2
        sbin_row = next(r for r in rows if r["instr_id"] != techm_instr_id)
        assert sbin_row["expd"] == "2026-05-26"
        assert sbin_row["strkp"] == 1200.0


class TestFoLatestPrices:
    """New coverage: F&O sync never wrote latest_prices before this round
    (an equity-only gap from Phase 4)."""

    def test_nse_fo_sync_writes_latest_price(self, sync_engine, db, monkeypatch):
        eq_df = pd.DataFrame([{"ISIN": "INE002A01018", "TckrSymb": "RELIANCE", "ClsPric": "2500.00"}])
        monkeypatch.setattr(nse_eq, "load_file_df", lambda *a, **k: eq_df)
        nse_eq._process_file(db, "eq.csv", "2026-09-25")
        db.commit()

        fo_df = pd.DataFrame([{
            "FinInstrmId": "9001", "TckrSymb": "RELIANCE", "FinInstrmTp": "STF",
            "XpryDt": "2026-09-25", "StrkPric": "0", "OptnTp": "",
            "ClsPric": "2510.00", "TradDt": "2026-09-25",
        }])
        monkeypatch.setattr(nse_fo, "load_file_chunks", lambda *a, **k: [fo_df])
        nse_fo._process_file(db, "fo.csv", "2026-09-25")
        db.commit()

        derivative_id = _derivatives_rows(sync_engine)[0]["instr_id"]
        latest_rows = _latest_price_rows(sync_engine)
        fo_latest = [r for r in latest_rows if r["instr_id"] == derivative_id]
        assert len(fo_latest) == 1
        assert fo_latest[0]["exchange"] == "NSE"
        assert fo_latest[0]["price"] == 2510.0

    def test_bse_fo_sync_writes_latest_price(self, sync_engine, db, monkeypatch):
        eq_df = pd.DataFrame([{"ISIN": "INE002A01018", "TckrSymb": "RELIANCE", "ClsPric": "2500.00"}])
        monkeypatch.setattr(nse_eq, "load_file_df", lambda *a, **k: eq_df)
        nse_eq._process_file(db, "eq.csv", "2026-09-25")
        db.commit()

        fo_df = pd.DataFrame([{
            "FinInstrmId": "9002", "TckrSymb": "RELIANCE", "FinInstrmTp": "STF",
            "XpryDt": "2026-09-25", "StrkPric": "0", "OptnTp": "",
            "ClsPric": "2520.00", "TradDt": "2026-09-25",
        }])
        monkeypatch.setattr(bse_fo, "load_file_df", lambda *a, **k: fo_df)
        bse_fo._process_file(db, "fo.csv", "2026-09-25")
        db.commit()

        derivative_id = _derivatives_rows(sync_engine)[0]["instr_id"]
        latest_rows = _latest_price_rows(sync_engine)
        fo_latest = [r for r in latest_rows if r["instr_id"] == derivative_id]
        assert len(fo_latest) == 1
        assert fo_latest[0]["exchange"] == "BSE"
        assert fo_latest[0]["price"] == 2520.0


class TestConcurrentSyncDoesNotRace:
    """Regression for a real production bug: NSE_EQ and BSE_EQ parsed at
    the same time both saw a newly-listed, dual-listed ISIN (ABB India) as
    "missing" and both tried to create instrument_equity rows for it,
    raising a UNIQUE constraint violation (whichever thread's INSERT landed
    second). instrument_write_lock must serialize each sync job's whole
    per-file resolve-through-commit window across threads, not just the
    resolve+create calls, or the race just moves to the gap between
    "unlocked" and "committed" instead of disappearing."""

    def test_nse_and_bse_sync_same_new_isin_concurrently(self, sync_engine, monkeypatch):
        nse_df = pd.DataFrame([{
            "ISIN": "INE117A01022", "TckrSymb": "ABB", "FinInstrmNm": "ABB INDIA LIMITED",
            "FinInstrmId": "19660", "ClsPric": "5000.00", "SctySrs": "EQ", "TradDt": "2026-09-28",
        }])
        bse_df = pd.DataFrame([{
            "ISIN": "INE117A01022", "TckrSymb": "ABB", "FinInstrmNm": "ABB INDIA LIMITED",
            "FinInstrmId": "500002", "ClsPric": "5005.00", "SctySrs": "A", "TradDt": "2026-09-28",
        }])
        monkeypatch.setattr(nse_eq, "load_file_df", lambda *a, **k: nse_df)
        monkeypatch.setattr(bse_eq, "load_file_df", lambda *a, **k: bse_df)

        SessionLocal = sessionmaker(bind=sync_engine)
        errors: list[Exception] = []
        barrier = threading.Barrier(2)

        def run_one(module, file_name):
            session = SessionLocal()
            db = Database(session)
            try:
                barrier.wait()  # try to maximize actual overlap before the lock
                with instrument_write_lock:
                    module._process_file(db, file_name, "2026-09-28")
                    db.commit()
            except Exception as exc:  # pragma: no cover - only hit if the fix regresses
                errors.append(exc)
            finally:
                session.close()

        t1 = threading.Thread(target=run_one, args=(nse_eq, "nse.csv"))
        t2 = threading.Thread(target=run_one, args=(bse_eq, "bse.csv"))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert errors == [], f"Concurrent sync raised: {errors!r}"

        rows = _equity_rows(sync_engine)
        assert len(rows) == 1  # one instrument, not two racing creates
        assert rows[0]["isin"] == "INE117A01022"
        # Whichever thread ran second must still have backfilled its own
        # exchange's fields onto the row the other thread created — this is
        # the exact gap a naive "INSERT OR IGNORE and move on" fix would
        # have left open.
        assert rows[0]["nse_sym"] == "ABB"
        assert rows[0]["bse_id"] == 500002


class TestForceRetriesFailedFiles:
    """Regression: `force` was accepted by every sync job's run() but never
    used anywhere — get_pending_files always filtered to status=DOWNLOADED
    only, so a file once marked SYNC_FAILED could never be picked up again
    through the API at all, force=True or not."""

    def _insert_file(self, sync_engine, file_name, status):
        with sync_engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO bhavcopy_files (file_name, trade_date, source, status, updated_at)
                VALUES (:fn, :td, :src, :status, datetime('now'))
            """), {"fn": file_name, "td": "2026-09-28", "src": "NSE_EQ", "status": status})

    def test_default_excludes_failed_files(self, sync_engine, db):
        self._insert_file(sync_engine, "ok.csv", int(FileStatus.DOWNLOADED))
        self._insert_file(sync_engine, "failed.csv", int(FileStatus.SYNC_FAILED))

        files = get_pending_files(db, "NSE_EQ")

        names = {f["file_name"] for f in files}
        assert names == {"ok.csv"}

    def test_include_failed_true_also_returns_failed_files(self, sync_engine, db):
        self._insert_file(sync_engine, "ok.csv", int(FileStatus.DOWNLOADED))
        self._insert_file(sync_engine, "failed.csv", int(FileStatus.SYNC_FAILED))

        files = get_pending_files(db, "NSE_EQ", include_failed=True)

        names = {f["file_name"] for f in files}
        assert names == {"ok.csv", "failed.csv"}

    def test_synced_files_are_never_returned_even_with_include_failed(self, sync_engine, db):
        self._insert_file(sync_engine, "done.csv", int(FileStatus.SYNCED))

        assert get_pending_files(db, "NSE_EQ", include_failed=True) == []
