from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.database import get_db

router = APIRouter()


class InstrumentRef(BaseModel):
    client_instrument_id: int
    isin:       Optional[str] = None
    nse_symbol: Optional[str] = None
    bse_code:   Optional[str] = None


@router.post("/resolve")
def resolve_instruments(
    refs: List[InstrumentRef],
    db: Session = Depends(get_db),
):
    """
    Given a list of instrument identifiers from the client (each with optional
    ISIN, NSE symbol, and/or BSE code), resolve each to a canonical server
    instrument record.

    Lookup priority per item: ISIN → NSE symbol → BSE code.

    Returns only items that were successfully resolved. Unresolved items are
    omitted — the client should treat those as unmapped.
    """
    resolved = []

    for ref in refs:
        row = None

        # 1. Try ISIN (most reliable)
        if ref.isin:
            row = db.execute(
                text("""
                    SELECT i.isin, i.name, ie.nse_symbol, ie.bse_code
                    FROM instruments i
                    LEFT JOIN instrument_equity ie ON ie.instrument_id = i.instrument_id
                    WHERE i.isin = :isin
                """),
                {"isin": ref.isin.upper()},
            ).mappings().first()

        # 2. Try NSE symbol
        if row is None and ref.nse_symbol:
            row = db.execute(
                text("""
                    SELECT i.isin, i.name, ie.nse_symbol, ie.bse_code
                    FROM instruments i
                    JOIN instrument_equity ie ON ie.instrument_id = i.instrument_id
                    WHERE UPPER(ie.nse_symbol) = :sym
                """),
                {"sym": ref.nse_symbol.upper()},
            ).mappings().first()

        # 3. Try BSE code
        if row is None and ref.bse_code:
            row = db.execute(
                text("""
                    SELECT i.isin, i.name, ie.nse_symbol, ie.bse_code
                    FROM instruments i
                    JOIN instrument_equity ie ON ie.instrument_id = i.instrument_id
                    WHERE ie.bse_code = :code
                """),
                {"code": ref.bse_code},
            ).mappings().first()

        if row and row["isin"]:
            resolved.append({
                "client_instrument_id": ref.client_instrument_id,
                "isin":       row["isin"],
                "name":       row["name"],
                "nse_symbol": row["nse_symbol"],
                "bse_code":   row["bse_code"],
            })

    return {"resolved": resolved}


@router.get("/search")
def search_instruments(
    q: str = Query(..., min_length=1, description="ISIN, symbol, or name"),
    db: Session = Depends(get_db),
):
    """Search instruments by ISIN, symbol, or name. Used by client during import."""
    results = db.execute(
        text("""
            SELECT i.instrument_id, i.isin, i.name, i.instrument_type_id,
                   it.name AS asset_class,
                   e.code AS exchange_code
            FROM instruments i
            JOIN instrument_types it ON i.instrument_type_id = it.instrument_type_id
            LEFT JOIN exchanges e ON i.primary_exchange_id = e.exchange_id
            WHERE i.isin = :q
               OR i.name LIKE :q_like
               OR EXISTS (
                   SELECT 1 FROM instrument_equity ie
                   WHERE ie.instrument_id = i.instrument_id
                     AND (ie.nse_symbol LIKE :q_like OR ie.bse_code = :q)
               )
            LIMIT 20
        """),
        {"q": q.upper(), "q_like": f"%{q}%"},
    ).mappings().all()

    return {"results": [dict(r) for r in results]}


@router.get("/{isin}")
def get_instrument(isin: str, db: Session = Depends(get_db)):
    """Get full instrument details by ISIN."""
    row = db.execute(
        text("""
            SELECT i.instrument_id, i.isin, i.name, i.instrument_type_id,
                   it.name AS asset_class, e.code AS exchange_code
            FROM instruments i
            JOIN instrument_types it ON i.instrument_type_id = it.instrument_type_id
            LEFT JOIN exchanges e ON i.primary_exchange_id = e.exchange_id
            WHERE i.isin = :isin
        """),
        {"isin": isin.upper()},
    ).mappings().first()

    if not row:
        raise HTTPException(status_code=404, detail="Instrument not found")

    return dict(row)
