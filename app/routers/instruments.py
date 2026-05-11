from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.database import get_db

router = APIRouter()

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

def _base(ref: PendingRef, row, **extra) -> dict:
    """Return a fully-populated resolved dict; all type-specific fields default to None."""
    return {
        "pending_id":               ref.pending_id,
        "instrument_id":            row["instrument_id"],
        "instrument_type_id":       row["instrument_type_id"],
        "instrument_type_name":     row["type"],
        "name":                     row["name"],
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
        "fo_instrument_type":       None,
        "fo_option_type":           None,
        "fo_nse_fininstrmid":       None,
        "fo_bse_fininstrmid":       None,
        "mcx_symbol":               None,
        "mcx_instrument_type":      None,
        "mcx_expiry_date":          None,
        "mcx_lot_size":             None,
        "mcx_unit":                 None,
        "mcx_strike_price_paise":   None,
        "mcx_option_type":          None,
        **extra,
    }


def _resolve_equity(ref: PendingRef, db: Session) -> Optional[dict]:
    row = None

    if ref.isin:
        row = db.execute(text("""
            SELECT i.instrument_id, i.instrument_type_id, it.name AS type, i.name,
                   ie.isin, ie.nse_symbol, ie.nse_fininstrmid, ie.bse_code
            FROM instruments i
            JOIN instrument_types it ON it.instrument_type_id = i.instrument_type_id
            JOIN instrument_equity ie ON ie.instrument_id = i.instrument_id
            WHERE ie.isin = :isin AND i.is_active = 1
        """), {"isin": ref.isin.upper()}).mappings().first()

    if row is None and ref.nse_symbol:
        row = db.execute(text("""
            SELECT i.instrument_id, i.instrument_type_id, it.name AS type, i.name,
                   ie.isin, ie.nse_symbol, ie.nse_fininstrmid, ie.bse_code
            FROM instruments i
            JOIN instrument_types it ON it.instrument_type_id = i.instrument_type_id
            JOIN instrument_equity ie ON ie.instrument_id = i.instrument_id
            WHERE UPPER(ie.nse_symbol) = :sym AND i.is_active = 1
        """), {"sym": ref.nse_symbol.upper()}).mappings().first()

    if row is None and ref.bse_code:
        row = db.execute(text("""
            SELECT i.instrument_id, i.instrument_type_id, it.name AS type, i.name,
                   ie.isin, ie.nse_symbol, ie.nse_fininstrmid, ie.bse_code
            FROM instruments i
            JOIN instrument_types it ON it.instrument_type_id = i.instrument_type_id
            JOIN instrument_equity ie ON ie.instrument_id = i.instrument_id
            WHERE ie.bse_code = :code AND i.is_active = 1
        """), {"code": ref.bse_code}).mappings().first()

    if not row:
        return None

    return _base(ref, row,
        isin=row["isin"],
        nse_symbol=row["nse_symbol"],
        nse_equity_fininstrmid=row["nse_fininstrmid"],
        bse_code=row["bse_code"],
    )


def _resolve_index(ref: PendingRef, db: Session) -> Optional[dict]:
    row = None
    sym = (ref.nse_symbol or "").upper()

    if sym and ref.exchange:
        row = db.execute(text("""
            SELECT i.instrument_id, i.instrument_type_id, it.name AS type, i.name,
                   ii.symbol, ii.exchange
            FROM instruments i
            JOIN instrument_types it ON it.instrument_type_id = i.instrument_type_id
            JOIN instrument_index ii ON ii.instrument_id = i.instrument_id
            WHERE UPPER(ii.symbol) = :sym AND UPPER(ii.exchange) = :exch
              AND i.is_active = 1
        """), {"sym": sym, "exch": ref.exchange.upper()}).mappings().first()

    if row is None and sym:
        row = db.execute(text("""
            SELECT i.instrument_id, i.instrument_type_id, it.name AS type, i.name,
                   ii.symbol, ii.exchange
            FROM instruments i
            JOIN instrument_types it ON it.instrument_type_id = i.instrument_type_id
            JOIN instrument_index ii ON ii.instrument_id = i.instrument_id
            WHERE UPPER(ii.symbol) = :sym AND i.is_active = 1
            LIMIT 1
        """), {"sym": sym}).mappings().first()

    if not row:
        return None

    return _base(ref, row,
        primary_exchange_code=row["exchange"],
        index_symbol=row["symbol"],
        index_exchange=row["exchange"],
    )


def _resolve_mf(ref: PendingRef, db: Session) -> Optional[dict]:
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

    return _base(ref, row,
        primary_exchange_code="AMFI",
        amfi_code=row["amfi_code"],
    )


def _resolve_fo(ref: PendingRef, db: Session) -> Optional[dict]:
    row = None

    if ref.nse_fininstrmid:
        row = db.execute(text("""
            SELECT i.instrument_id, i.instrument_type_id, it.name AS type, i.name,
                   ifo.underlying_instrument_id, ifo.underlying_symbol,
                   ifo.instrument_type AS fo_instrument_type,
                   ifo.option_type AS fo_option_type,
                   ifo.expiry_date, ifo.strike_price_paise,
                   ifo.lot_size, ifo.nse_fininstrmid, ifo.bse_fininstrmid
            FROM instruments i
            JOIN instrument_types it ON it.instrument_type_id = i.instrument_type_id
            JOIN instrument_derivatives ifo ON ifo.instrument_id = i.instrument_id
            WHERE ifo.nse_fininstrmid = :fid AND i.is_active = 1
        """), {"fid": ref.nse_fininstrmid}).mappings().first()

    if row is None and ref.underlying_symbol and ref.expiry_date:
        itype = (ref.contract_type or "FUTURES").upper()
        params: dict = {
            "sym":   ref.underlying_symbol.upper(),
            "exp":   ref.expiry_date,
            "itype": itype,
        }
        strike_clause = ""
        if ref.strike_price_paise is not None:
            params["strike"] = ref.strike_price_paise
            strike_clause = " AND ifo.strike_price_paise = :strike"

        row = db.execute(text(f"""
            SELECT i.instrument_id, i.instrument_type_id, it.name AS type, i.name,
                   ifo.underlying_instrument_id, ifo.underlying_symbol,
                   ifo.instrument_type AS fo_instrument_type,
                   ifo.option_type AS fo_option_type,
                   ifo.expiry_date, ifo.strike_price_paise,
                   ifo.lot_size, ifo.nse_fininstrmid, ifo.bse_fininstrmid
            FROM instruments i
            JOIN instrument_types it ON it.instrument_type_id = i.instrument_type_id
            JOIN instrument_derivatives ifo ON ifo.instrument_id = i.instrument_id
            WHERE UPPER(ifo.underlying_symbol) = :sym
              AND ifo.expiry_date = :exp
              AND ifo.instrument_type = :itype
              AND i.is_active = 1{strike_clause}
            LIMIT 1
        """), params).mappings().first()

    if not row:
        return None

    return _base(ref, row,
        underlying_instrument_id=row["underlying_instrument_id"],
        underlying_symbol=row["underlying_symbol"],
        fo_expiry_date=row["expiry_date"],
        fo_lot_size=row["lot_size"],
        fo_strike_price_paise=row["strike_price_paise"],
        fo_instrument_type=row["fo_instrument_type"],
        fo_option_type=row["fo_option_type"],
        fo_nse_fininstrmid=row["nse_fininstrmid"],
        fo_bse_fininstrmid=row["bse_fininstrmid"],
    )


def _resolve_mcx(ref: PendingRef, db: Session) -> Optional[dict]:
    symbol = (ref.mcx_symbol or ref.underlying_symbol or "").upper()
    if not symbol or not ref.expiry_date:
        return None

    row = db.execute(text("""
        SELECT i.instrument_id, i.instrument_type_id, it.name AS type, i.name,
               imcx.mcx_symbol, imcx.instrument_type AS mcx_instrument_type,
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

    return _base(ref, row,
        primary_exchange_code="MCX",
        mcx_symbol=row["mcx_symbol"],
        mcx_instrument_type=row["mcx_instrument_type"],
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
def get_instrument_types(db: Session = Depends(get_db)):
    """Return all instrument types (server-mastered reference data)."""
    rows = db.execute(
        text("""
            SELECT instrument_type_id, name, asset_class, tax_category
            FROM instrument_types
            ORDER BY instrument_type_id
        """),
    ).mappings().all()
    return {"instrument_types": [dict(r) for r in rows]}


@router.get("/updates")
def get_instrument_updates(
    instrument_ids: List[int] = Query(None, description="Filter by instrument_ids"),
    since: Optional[str] = Query(None, description="ISO datetime e.g. 2026-04-16T10:30:00"),
    db: Session = Depends(get_db),
):
    """
    Return instrument metadata for delta sync.

    Returns instruments (optionally filtered by ids) updated since `since`.
    Includes key extension fields for equity/mf/mcx/index.
    """
    params: dict = {}

    id_filter = ""
    if instrument_ids:
        placeholders = ",".join(f":id_{i}" for i in range(len(instrument_ids)))
        params.update({f"id_{i}": iid for i, iid in enumerate(instrument_ids)})
        id_filter = f"WHERE i.instrument_id IN ({placeholders})"

    since_filter = ""
    if since:
        params["since"] = since
        clause = "AND" if id_filter else "WHERE"
        since_filter = f"{clause} i.updated_at > :since"

    rows = db.execute(
        text(f"""
            SELECT
                i.instrument_id,
                i.instrument_type_id,
                it.name AS instrument_type_name,
                i.name,
                i.updated_at,
                ie.isin,
                ie.nse_symbol,
                ie.bse_code,
                ie.sector,
                ie.industry,
                ii.symbol AS index_symbol,
                ii.exchange AS index_exchange,
                imf.amfi_code,
                imcx.mcx_symbol
            FROM instruments i
            JOIN instrument_types it ON it.instrument_type_id = i.instrument_type_id
            LEFT JOIN instrument_equity ie ON ie.instrument_id = i.instrument_id
            LEFT JOIN instrument_index ii ON ii.instrument_id = i.instrument_id
            LEFT JOIN instrument_mf imf ON imf.instrument_id = i.instrument_id
            LEFT JOIN instrument_mcx imcx ON imcx.instrument_id = i.instrument_id
            {id_filter}
            {since_filter}
            ORDER BY i.updated_at
        """),
        params,
    ).mappings().all()

    synced_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    return {"updates": [dict(r) for r in rows], "synced_at": synced_at}


@router.post("/resolve")
def resolve_instruments(
    refs: List[PendingRef],
    db: Session = Depends(get_db),
):
    """
    Resolve pending instruments from the client.

    Each ref carries a pending_id (local staging row ID) and type-specific
    lookup fields. Resolution priority per type:
      EQUITY           → isin → nse_symbol → bse_code
      INDEX            → (symbol, exchange) → symbol alone
      MF variants      → amfi_code → isin
      FUTURES/OPTIONS  → nse_fininstrmid → (underlying_symbol, expiry, strike)
      COMMODITY_*      → (mcx_symbol, expiry_date)

    Returns only resolved items; unresolved are omitted — the client retries
    on the next sync cycle.
    """
    resolved = []
    for ref in refs:
        itype = ref.instrument_type.upper()
        if itype == "EQUITY":
            result = _resolve_equity(ref, db)
        elif itype == "INDEX":
            result = _resolve_index(ref, db)
        elif itype in MF_TYPES:
            result = _resolve_mf(ref, db)
        elif itype in FO_TYPES:
            result = _resolve_fo(ref, db)
        elif itype in MCX_TYPES:
            result = _resolve_mcx(ref, db)
        else:
            result = None
        if result:
            resolved.append(result)
    return {"resolved": resolved}


@router.get("/search")
def search_instruments(
    q: str = Query(..., min_length=1, description="ISIN, symbol, or name"),
    db: Session = Depends(get_db),
):
    """Search instruments by ISIN, symbol, or name."""
    results = db.execute(
        text("""
            SELECT i.instrument_id, i.name, it.name AS asset_class,
                   ie.isin, ie.nse_symbol, ie.bse_code
            FROM instruments i
            JOIN instrument_types it ON it.instrument_type_id = i.instrument_type_id
            LEFT JOIN instrument_equity ie ON ie.instrument_id = i.instrument_id
            WHERE ie.isin = :q
               OR i.name LIKE :q_like
               OR UPPER(ie.nse_symbol) LIKE :q_like
               OR ie.bse_code = :q
            LIMIT 20
        """),
        {"q": q.upper(), "q_like": f"%{q}%"},
    ).mappings().all()
    return {"results": [dict(r) for r in results]}


@router.post("/equity")
def create_equity_instrument(req: CreateEquityRequest, db: Session = Depends(get_db)):
    """
    Manually add an equity instrument to the server catalog.

    Useful for adding instruments that are missing from the NSE/BSE bhavcopy
    (e.g. old ISINs superseded by a stock split, delisted securities, etc.).
    On the next client sync, pending instruments matching this ISIN/symbol
    will be resolved automatically.

    Returns the instrument_id whether the instrument was newly created or
    already existed (idempotent on isin / nse_symbol / bse_code).
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    # Normalise identifiers
    isin       = req.isin.upper().strip()       if req.isin       else None
    nse_symbol = req.nse_symbol.upper().strip() if req.nse_symbol else None
    bse_code   = req.bse_code.strip()           if req.bse_code   else None

    # Check if already exists
    existing = db.execute(text("""
        SELECT i.instrument_id, i.name,
               ie.isin, ie.nse_symbol, ie.nse_fininstrmid, ie.bse_code,
               ie.face_value_paise, ie.sector, ie.industry
        FROM instruments i
        JOIN instrument_equity ie ON ie.instrument_id = i.instrument_id
        WHERE (:isin IS NOT NULL AND ie.isin = :isin)
           OR (:nse  IS NOT NULL AND UPPER(ie.nse_symbol) = :nse)
           OR (:bse  IS NOT NULL AND ie.bse_code = :bse)
        LIMIT 1
    """), {"isin": isin, "nse": nse_symbol, "bse": bse_code}).mappings().first()

    if existing:
        return {
            "instrument_id":   existing["instrument_id"],
            "created":         False,
            "name":            existing["name"],
            "isin":            existing["isin"],
            "nse_symbol":      existing["nse_symbol"],
            "nse_fininstrmid": existing["nse_fininstrmid"],
            "bse_code":        existing["bse_code"],
            "face_value_paise": existing["face_value_paise"],
            "sector":          existing["sector"],
            "industry":        existing["industry"],
        }

    # Look up the EQUITY instrument_type_id
    type_row = db.execute(text("""
        SELECT instrument_type_id FROM instrument_types WHERE name = 'EQUITY' LIMIT 1
    """)).mappings().first()
    if not type_row:
        raise HTTPException(status_code=500, detail="EQUITY instrument type not configured on server")

    nse_exchange_row = db.execute(text("""
        SELECT exchange_id FROM exchanges WHERE code = 'NSE' LIMIT 1
    """)).mappings().first()
    primary_exchange_id = nse_exchange_row["exchange_id"] if nse_exchange_row else None

    from app.database import engine
    with engine.begin() as conn:
        result = conn.execute(text("""
            INSERT INTO instruments (name, instrument_type_id, primary_exchange_id,
                                     is_active, source, created_at, updated_at)
            VALUES (:name, :type_id, :exch_id, 1, 'MANUAL', :now, :now)
        """), {
            "name":    req.name.strip(),
            "type_id": type_row["instrument_type_id"],
            "exch_id": primary_exchange_id,
            "now":     now,
        })
        instrument_id = result.lastrowid

        conn.execute(text("""
            INSERT INTO instrument_equity
                (instrument_id, isin, nse_symbol, nse_fininstrmid, bse_code,
                 face_value_paise, sector, industry)
            VALUES (:iid, :isin, :nse, :nse_fin, :bse, :fv, :sector, :industry)
        """), {
            "iid":     instrument_id,
            "isin":    isin,
            "nse":     nse_symbol,
            "nse_fin": req.nse_fininstrmid,
            "bse":     bse_code,
            "fv":      req.face_value_paise,
            "sector":  req.sector,
            "industry": req.industry,
        })

    return {
        "instrument_id":  instrument_id,
        "created":        True,
        "name":           req.name.strip(),
        "isin":           isin,
        "nse_symbol":     nse_symbol,
        "nse_fininstrmid": req.nse_fininstrmid,
        "bse_code":       bse_code,
        "face_value_paise": req.face_value_paise,
        "sector":         req.sector,
        "industry":       req.industry,
    }


@router.get("/{isin}")
def get_instrument(isin: str, db: Session = Depends(get_db)):
    """Get instrument details by ISIN."""
    row = db.execute(
        text("""
            SELECT i.instrument_id, i.name, it.name AS asset_class,
                   ie.isin, ie.nse_symbol, ie.bse_code
            FROM instruments i
            JOIN instrument_types it ON it.instrument_type_id = i.instrument_type_id
            JOIN instrument_equity ie ON ie.instrument_id = i.instrument_id
            WHERE ie.isin = :isin
        """),
        {"isin": isin.upper()},
    ).mappings().first()

    if not row:
        raise HTTPException(status_code=404, detail="Instrument not found")

    return dict(row)
