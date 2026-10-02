import logging
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from arthdesk_db import Database
from sqlalchemy import text, select
from arthdesk_instruments import dao
from app.database import get_db
from app.routers.deps import require_active_subscription
from app.tables import instrument_types, asset_classes

router = APIRouter()
logger = logging.getLogger(__name__)

MF_TYPES  = {"EQUITY_MF", "DEBT_MF", "HYBRID_MF", "ELSS", "SIF"}
FO_TYPES  = {"FUTURES", "OPTIONS"}
MCX_TYPES = {"COMMODITY_FUTURES", "COMMODITY_OPTIONS"}


# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------

class PendingRef(BaseModel):
    pending_id:         int
    instrument_type:    str
    name:               Optional[str] = None
    isin:               Optional[str] = None
    nse_symbol:         Optional[str] = None
    bse_code:           Optional[str] = None
    exchange:           Optional[str] = None
    amfi_code:          Optional[str] = None
    nse_fininstrmid:    Optional[int] = None
    underlying_symbol:  Optional[str] = None
    expiry_date:        Optional[str] = None
    strike_price_paise: Optional[int] = None
    contract_type:      Optional[str] = None
    mcx_symbol:         Optional[str] = None
    unit:               Optional[str] = None


class CreateEquityRequest(BaseModel):
    name:               str
    isin:               Optional[str] = None
    nse_symbol:         Optional[str] = None
    nse_fininstrmid:    Optional[int] = None
    bse_code:           Optional[str] = None
    face_value_paise:   Optional[int] = None
    sector:             Optional[str] = None
    industry:           Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base(ref: PendingRef, instrument_id: int, instrument_type_name: str, name: str, **extra) -> dict:
    """Return a fully-populated resolved dict; all type-specific fields default to None."""
    return {
        "pending_id":               ref.pending_id,
        "instrument_id":            instrument_id,
        "instrument_type_name":     instrument_type_name,
        "name":                     name,
        "primary_exchange_code":    None,
        "isin":                     None,
        "nse_symbol":               None,
        "nse_equity_fininstrmid":   None,
        "bse_code":                 None,
        "amfi_code":                None,
        "index_symbol":             None,
        "index_exchange":           None,
        "underlying_instrument_id": None,
        "underlying_symbol":        None,
        "fo_expiry_date":           None,
        "fo_lot_size":              None,
        "fo_strike_price_paise":    None,
        "fo_contract_type":         None,
        "fo_option_type":           None,
        "fo_nse_fininstrmid":       None,
        "fo_bse_fininstrmid":       None,
        "mcx_symbol":               None,
        "mcx_contract_type":        None,
        "mcx_expiry_date":          None,
        "mcx_lot_size":             None,
        "mcx_unit":                 None,
        "mcx_strike_price_paise":   None,
        "mcx_option_type":          None,
        **extra,
    }


def _resolve_equity_via_dao(ref: PendingRef, db: Database) -> Optional[dict]:
    row = dao.resolve(db, "EQUITY", isin=ref.isin, nse_sym=ref.nse_symbol,
                       nse_id=ref.nse_fininstrmid, bse_sym=ref.bse_code)
    if not row:
        return None
    name = row.get("nse_name") or row.get("bse_name") or ref.isin
    return _base(ref, row["instr_id"], "EQUITY", name,
        isin=row["isin"],
        nse_symbol=row["nse_sym"],
        nse_equity_fininstrmid=row["nse_id"],
        bse_code=row["bse_id"],
    )


def _resolve_index_via_dao(ref: PendingRef, db: Database) -> Optional[dict]:
    sym = (ref.nse_symbol or "").upper()
    if not sym:
        return None
    row = dao.resolve(db, "INDEX", sym=sym)
    if not row:
        return None
    return _base(ref, row["instr_id"], "INDEX", row["sym"],
        primary_exchange_code=ref.exchange,
        index_symbol=row["sym"],
        index_exchange=ref.exchange,
    )


def _resolve_fo_via_dao(ref: PendingRef, db: Database) -> Optional[dict]:
    ul_instr_id = None
    if ref.underlying_symbol and ref.exchange:
        ul_instr_id = dao.derivatives.resolve_underlying(db, ref.underlying_symbol, ref.exchange)

    strkp = ref.strike_price_paise / 100 if ref.strike_price_paise is not None else None
    row = dao.resolve(db, "DERIVATIVES", exchange=ref.exchange, nse_bse_id=ref.nse_fininstrmid,
                       ul_instr_id=ul_instr_id, expd=ref.expiry_date, strkp=strkp,
                       opn_type=ref.contract_type)
    if not row:
        return None

    nse_fin = row["nse_bse_id"] if row["exchange"] == "NSE" else None
    bse_fin = row["nse_bse_id"] if row["exchange"] == "BSE" else None
    return _base(ref, row["instr_id"], "DERIVATIVES", row.get("nse_bse_name") or "",
        underlying_instrument_id=row["ul_instr_id"],
        underlying_symbol=ref.underlying_symbol,
        fo_expiry_date=row["expd"],
        fo_lot_size=row["lot_size"],
        fo_strike_price_paise=int(round(row["strkp"] * 100)) if row["strkp"] is not None else None,
        fo_contract_type=row["contract_type"],
        fo_option_type=row["opn_type"],
        fo_nse_fininstrmid=nse_fin,
        fo_bse_fininstrmid=bse_fin,
    )


def _resolve_mf(ref: PendingRef, db: Database) -> Optional[dict]:
    """Not migrated this round (dao.mf doesn't exist yet) — references the
    old schema, will raise if actually invoked. Call site wraps this in
    try/except so it can't take down resolution of other refs in a batch."""
    row = None

    if ref.amfi_code:
        row = db.execute(text("""
            SELECT i.instrument_id, i.instrument_type_id, it.name AS type, i.name,
                   imf.amfi_code
            FROM instruments i
            JOIN instrument_types it ON it.instrument_type_id = i.instrument_type_id
            JOIN instrument_mf imf ON imf.instrument_id = i.instrument_id
            WHERE imf.amfi_code = :code AND i.is_active = 1
        """), {"code": ref.amfi_code}).mappings().first()

    if row is None and ref.isin:
        row = db.execute(text("""
            SELECT i.instrument_id, i.instrument_type_id, it.name AS type, i.name,
                   imf.amfi_code
            FROM instruments i
            JOIN instrument_types it ON it.instrument_type_id = i.instrument_type_id
            JOIN instrument_mf imf ON imf.instrument_id = i.instrument_id
            WHERE imf.isin = :isin AND i.is_active = 1
        """), {"isin": ref.isin.upper()}).mappings().first()

    if not row:
        return None

    return _base(ref, row["instrument_id"], row["type"], row["name"],
        primary_exchange_code="AMFI",
        amfi_code=row["amfi_code"],
    )


def _resolve_mcx(ref: PendingRef, db: Database) -> Optional[dict]:
    """Not migrated this round (dao.mcx doesn't exist yet) — references the
    old schema, will raise if actually invoked. Call site wraps this in
    try/except so it can't take down resolution of other refs in a batch."""
    symbol = (ref.mcx_symbol or ref.underlying_symbol or "").upper()
    if not symbol or not ref.expiry_date:
        return None

    row = db.execute(text("""
        SELECT i.instrument_id, i.instrument_type_id, it.name AS type, i.name,
               imcx.mcx_symbol, imcx.contract_type AS mcx_contract_type,
               imcx.expiry_date, imcx.strike_price_paise, imcx.option_type,
               imcx.lot_size, imcx.unit
        FROM instruments i
        JOIN instrument_types it ON it.instrument_type_id = i.instrument_type_id
        JOIN instrument_mcx imcx ON imcx.instrument_id = i.instrument_id
        WHERE UPPER(imcx.mcx_symbol) = :sym AND imcx.expiry_date = :exp
          AND i.is_active = 1
        LIMIT 1
    """), {"sym": symbol, "exp": ref.expiry_date}).mappings().first()

    if not row:
        return None

    return _base(ref, row["instrument_id"], row["type"], row["name"],
        primary_exchange_code="MCX",
        mcx_symbol=row["mcx_symbol"],
        mcx_contract_type=row["mcx_contract_type"],
        mcx_expiry_date=row["expiry_date"],
        mcx_lot_size=row["lot_size"],
        mcx_unit=row["unit"],
        mcx_strike_price_paise=row["strike_price_paise"],
        mcx_option_type=row["option_type"],
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/types")
def get_instrument_types(db: Database = Depends(get_db)):
    """Return all instrument types (server-mastered reference data)."""
    stmt = select(instrument_types).order_by(instrument_types.c.id)
    return {"instrument_types": db.fetch_all(stmt)}


@router.get("/asset-classes")
def get_asset_classes(db: Database = Depends(get_db)):
    """
    Return the canonical asset_class codes (server-mastered reference data).

    Codes match arthdesk-py's backend/enums.py::AssetClass exactly — desktop
    clients sync this list rather than hardcoding their own copy, so the two
    can never drift the way 'MF'/'MUTUAL_FUND' and 'DERIVATIVE'/'DERIVATIVES'
    previously did.
    """
    stmt = select(asset_classes).order_by(asset_classes.c.code)
    return {"asset_classes": db.fetch_all(stmt)}


@router.get("/updates", dependencies=[Depends(require_active_subscription)])
def get_instrument_updates(
    instrument_ids: List[int] = Query(None, description="Filter by instrument_ids"),
    since: Optional[str] = Query(None, description="ISO datetime e.g. 2026-04-16T10:30:00"),
    db: Database = Depends(get_db),
):
    """
    Not migrated this round. This is a genuinely cross-table operation
    (LEFT JOIN across every detail table) that belongs in a future dao.updates
    module (arthdesk-instruments), not an improvised rewrite here that would
    get thrown away once that module exists. Failing clearly rather than
    letting it hit the old schema and raise an opaque SQL error.
    """
    raise HTTPException(status_code=501, detail="Instrument delta sync is temporarily unavailable "
                                                  "pending the shared instruments package's updates module.")


@router.post("/resolve", dependencies=[Depends(require_active_subscription)])
def resolve_instruments(
    refs: List[PendingRef],
    db: Database = Depends(get_db),
):
    """
    Resolve pending instruments from the client.

    Each ref carries a pending_id (local staging row ID) and type-specific
    lookup fields. Resolution priority per type:
      EQUITY           → isin → nse_id → bse_id → nse_sym → bse_sym (via dao.equity)
      INDEX            → sym (via dao.index)
      MF variants      → amfi_code → isin (not migrated — old schema, may fail)
      FUTURES/OPTIONS  → exchange+id → underlying+expiry+strike+type (via dao.derivatives)
      COMMODITY_*      → (mcx_symbol, expiry_date) (not migrated — old schema, may fail)

    Returns only resolved items; unresolved are omitted — the client retries
    on the next sync cycle. A failure resolving one ref (e.g. an MF/MCX ref
    hitting the not-yet-migrated schema) is caught and logged rather than
    failing the whole batch.
    """
    resolved = []
    for ref in refs:
        itype = ref.instrument_type.upper()
        try:
            if itype == "EQUITY":
                result = _resolve_equity_via_dao(ref, db)
            elif itype == "INDEX":
                result = _resolve_index_via_dao(ref, db)
            elif itype in MF_TYPES:
                result = _resolve_mf(ref, db)
            elif itype in FO_TYPES:
                result = _resolve_fo_via_dao(ref, db)
            elif itype in MCX_TYPES:
                result = _resolve_mcx(ref, db)
            else:
                result = None
        except Exception:
            logger.exception("Failed to resolve pending_id=%s instrument_type=%s", ref.pending_id, itype)
            result = None
        if result:
            resolved.append(result)
    return {"resolved": resolved}


@router.get("/search", dependencies=[Depends(require_active_subscription)])
def search_instruments(
    q: str = Query(..., min_length=1, description="ISIN, symbol, name, or AMFI code"),
    asset_class: Optional[str] = Query(
        None,
        description=(
            "Filter by asset class: 'EQUITY', 'MUTUAL_FUND', 'INDEX', 'FIXED_INCOME', "
            "'DERIVATIVES', 'COMMODITY' — see the asset_classes table. "
            "Omit to search across all classes."
        ),
    ),
    db: Database = Depends(get_db),
):
    """
    Search instruments by ISIN, symbol, name, or AMFI code.

    asset_class values (both query param and response) match asset_classes.code
    exactly — the same codes arthdesk-py's backend/enums.py::AssetClass uses.
    """
    _ac = (asset_class or "").upper()
    results = []

    # ── EQUITY branch (migrated) ─────────────────────────────────────────────
    if not _ac or _ac == "EQUITY":
        for row in dao.equity.search(db, q):
            results.append({
                "instrument_id": row["instr_id"],
                "name": row.get("nse_name") or row.get("bse_name"),
                "type_code": "EQUITY",
                "asset_class": "EQUITY",
                "isin": row["isin"],
                "nse_symbol": row["nse_sym"],
                "bse_code": row["bse_id"],
                "amfi_code": None,
                "fund_house": None,
            })

    # ── MUTUAL FUND branch (not migrated — old schema, may fail) ─────────────
    if not _ac or _ac == "MUTUAL_FUND":
        try:
            rows = db.execute(
                text("""
                    SELECT
                        i.instrument_id,
                        i.name,
                        it.name        AS type_code,
                        it.asset_class AS asset_class,
                        imf.isin,
                        NULL           AS nse_symbol,
                        NULL           AS bse_code,
                        imf.amfi_code,
                        imf.fund_house
                    FROM instruments i
                    JOIN instrument_types it ON it.instrument_type_id = i.instrument_type_id
                    JOIN instrument_mf imf ON imf.instrument_id = i.instrument_id
                    WHERE it.asset_class = 'MUTUAL_FUND'
                      AND i.is_active = 1
                      AND (
                          imf.isin = :q
                          OR imf.amfi_code = :q
                          OR i.name LIKE :q_like
                          OR imf.fund_house LIKE :q_like
                      )
                    ORDER BY
                        CASE WHEN imf.isin = :q THEN 0
                             WHEN imf.amfi_code = :q THEN 1
                             ELSE 2
                        END,
                        i.name
                    LIMIT 20
                """),
                {"q": q.upper(), "q_like": f"%{q}%"},
            ).mappings().all()
            results.extend(dict(r) for r in rows)
        except Exception:
            logger.exception("MF search failed (not migrated to arthdesk_instruments yet)")

    return {"results": results}


@router.post("/equity")
def create_equity_instrument(req: CreateEquityRequest, db: Database = Depends(get_db)):
    """
    Manually add an equity instrument to the server catalog.

    Useful for adding instruments that are missing from the NSE/BSE bhavcopy
    (e.g. old ISINs superseded by a stock split, delisted securities, etc.).
    On the next client sync, pending instruments matching this ISIN/symbol
    will be resolved automatically.

    Returns the instrument_id whether the instrument was newly created or
    already existed (idempotent on isin / nse_symbol / bse_code).
    """
    isin       = req.isin.upper().strip()       if req.isin       else None
    nse_symbol = req.nse_symbol.upper().strip() if req.nse_symbol else None
    bse_code   = req.bse_code.strip()           if req.bse_code   else None

    result = dao.equity.create(
        db, isin=isin, nse_sym=nse_symbol, nse_id=req.nse_fininstrmid,
        nse_name=req.name.strip(), bse_sym=bse_code,
        face_value=req.face_value_paise / 100 if req.face_value_paise is not None else None,
    )
    db.commit()

    return {
        "instrument_id":    result["instr_id"],
        "created":          result["created"],
        "name":             result.get("nse_name") or req.name.strip(),
        "isin":             result["isin"],
        "nse_symbol":       result["nse_sym"],
        "nse_fininstrmid":  result["nse_id"],
        "bse_code":         result["bse_id"],
        "face_value_paise": int(round(result["face_value"] * 100)) if result.get("face_value") is not None else None,
        "sector":           None,   # dropped from the schema in Phase 2 — no longer tracked
        "industry":         None,
    }


@router.get("/{isin}")
def get_instrument(isin: str, db: Database = Depends(get_db)):
    """Get instrument details by ISIN."""
    row = dao.equity.resolve(db, isin=isin.upper())
    if not row:
        raise HTTPException(status_code=404, detail="Instrument not found")

    return {
        "instrument_id": row["instr_id"],
        "name":          row.get("nse_name") or row.get("bse_name"),
        "asset_class":   "EQUITY",
        "isin":          row["isin"],
        "nse_symbol":    row["nse_sym"],
        "bse_code":      row["bse_id"],
    }
