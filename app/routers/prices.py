from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.database import get_db
from app import cache
from app.routers.deps import require_active_subscription

router = APIRouter()


@router.get("/latest")
def latest_prices(
    instrument_ids: List[int] = Query(None, description="List of server instrument_ids"),
    isins: List[str] = Query(None, description="[Legacy] List of ISINs the client holds"),
    _user: dict = Depends(require_active_subscription),
):
    """
    Return today's latest price (OHLC) for each ISIN.
    Served from in-memory cache — no DB hit after first request of the day.
    """
    today = date.today()
    cached = cache.get_prices(today)

    if cached is not None:
        result = {}
        if instrument_ids:
            # Preferred path: fetch by instrument_ids
            result.update({iid: cached.get(iid) for iid in instrument_ids if iid in cached})
            
        if isins:
            # Legacy path: cache no longer uses ISINs as keys, returning empty for these
            pass
            
        return {"date": today.isoformat(), "prices": result}

    return {"date": today.isoformat(), "prices": {}, "cache_miss": True}


@router.get("/sync")
def sync_prices(
    instrument_ids: List[int] = Query(
        None,
        description="List of server instrument_ids to fetch prices for (preferred).",
    ),
    isins: List[str] = Query(
        None,
        description="[Legacy] List of ISINs. Use instrument_ids instead.",
    ),
    since_date: Optional[str] = Query(
        None,
        description="[Legacy] Return EOD prices after this YYYY-MM-DD date.",
    ),
    since_datetime: Optional[str] = Query(
        None,
        description="Return latest_prices rows where last_synced_at > this ISO datetime. "
                    "Format: YYYY-MM-DDTHH:MM:SS  e.g. 2026-04-16T10:30:00",
    ),
    db: Session = Depends(get_db),
    _user: dict = Depends(require_active_subscription),
):
    """
    Incremental price sync.

    Preferred mode — **instrument_ids**: query latest_prices directly by
    instrument_id. Returns prices keyed by instrument_id (no join needed).

    Legacy mode — **isins**: retained for backward compatibility.

    In both modes, pass since_datetime for incremental sync (only rows that
    changed since that timestamp are returned).

    Typical client flow:
      1. On first launch: call with instrument_ids, no since_datetime.
      2. Store the synced_at timestamp returned in the response.
      3. On subsequent calls: pass that timestamp as since_datetime.
    """
    synced_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")

    # ── Instrument-ID path (new preferred) ───────────────────────────────────
    if instrument_ids:
        placeholders = ",".join(f":id_{i}" for i in range(len(instrument_ids)))
        params: dict = {f"id_{i}": iid for i, iid in enumerate(instrument_ids)}

        date_filter = ""
        if since_datetime is not None:
            params["since_dt"] = since_datetime
            date_filter = "AND lp.last_synced_at > :since_dt"

        rows = db.execute(
            text(f"""
                SELECT
                    lp.instrument_id,
                    lp.price_date,
                    lp.open_price_paise,
                    lp.high_price_paise,
                    lp.low_price_paise,
                    lp.close_price_paise,
                    lp.last_synced_at
                FROM latest_prices lp
                WHERE lp.instrument_id IN ({placeholders})
                {date_filter}
                ORDER BY lp.last_synced_at DESC
            """),
            params,
        ).mappings().all()

        return {"prices": [dict(r) for r in rows], "synced_at": synced_at}

    # ── Legacy ISIN path ──────────────────────────────────────────────────────
    if not isins:
        return {"prices": [], "synced_at": None}

    named_placeholders = ",".join(f":isin_{i}" for i in range(len(isins)))
    params = {f"isin_{i}": isin for i, isin in enumerate(isins)}

    if since_date is None:
        date_filter = ""
        if since_datetime is not None:
            params["since_dt"] = since_datetime
            date_filter = "AND lp.last_synced_at > :since_dt"

        rows = db.execute(
            text(f"""
                SELECT
                    ie.isin,
                    lp.price_date,
                    lp.open_price_paise,
                    lp.high_price_paise,
                    lp.low_price_paise,
                    lp.close_price_paise,
                    lp.last_synced_at
                FROM latest_prices lp
                JOIN instrument_equity ie ON ie.instrument_id = lp.instrument_id
                WHERE ie.isin IN ({named_placeholders})
                {date_filter}
                ORDER BY lp.last_synced_at DESC
            """),
            params,
        ).mappings().all()

        return {"prices": [dict(r) for r in rows], "synced_at": synced_at}

    # ── Historical EOD sync (legacy, only when since_date is explicitly set) ──
    params["since_date"] = since_date
    rows = db.execute(
        text(f"""
            SELECT
                ie.isin,
                eod.trade_date  AS price_date,
                eod.open_price_paise,
                eod.high_price_paise,
                eod.low_price_paise,
                eod.close_price_paise
            FROM equity_eod eod
            JOIN instrument_equity ie ON ie.instrument_id = eod.instrument_id
            WHERE ie.isin IN ({named_placeholders})
              AND eod.trade_date > :since_date
            ORDER BY ie.isin, eod.trade_date
        """),
        params,
    ).mappings().all()

    return {"prices": [dict(r) for r in rows], "synced_at": synced_at}


@router.get("/trading-calendar")
def trading_calendar(
    year: int = Query(..., description="Calendar year e.g. 2026"),
    db: Session = Depends(get_db),
):
    """Return market holidays for a given year."""
    rows = db.execute(
        text("""
            SELECT trade_date, description
            FROM trading_calendar
            WHERE trade_date LIKE :year_prefix
            ORDER BY trade_date
        """),
        {"year_prefix": f"{year}-%"},
    ).mappings().all()

    return {"holidays": [dict(r) for r in rows]}
