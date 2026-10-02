from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from arthdesk_db import Database
from arthdesk_instruments import dao
from sqlalchemy import text

from app.database import get_db
from app import cache
from app.routers.deps import require_active_subscription

router = APIRouter()


@router.get("/latest", dependencies=[Depends(require_active_subscription)])
def latest_prices(
    instrument_ids: List[int] = Query(None, description="List of server instrument_ids"),
):
    """
    Return today's latest price for each instrument_id.
    Served from in-memory cache — no DB hit after first request of the day.
    """
    today = date.today()
    cached = cache.get_prices(today)

    if cached is not None:
        result = {}
        if instrument_ids:
            result.update({iid: cached.get(iid) for iid in instrument_ids if iid in cached})
        return {"date": today.isoformat(), "prices": result}

    return {"date": today.isoformat(), "prices": {}, "cache_miss": True}


@router.get("/sync", dependencies=[Depends(require_active_subscription)])
def sync_prices(
    instrument_ids: List[int] = Query(
        [],
        description="List of server instrument_ids to fetch prices for.",
    ),
    since_datetime: Optional[str] = Query(
        None,
        description="Return latest_prices rows where last_synced_at > this ISO datetime. "
                    "Format: YYYY-MM-DDTHH:MM:SS  e.g. 2026-04-16T10:30:00",
    ),
    db: Database = Depends(get_db),
):
    """
    Incremental price sync, keyed by instrument_id.

    Typical client flow:
      1. On first launch: call with instrument_ids, no since_datetime.
      2. Store the synced_at timestamp returned in the response.
      3. On subsequent calls: pass that timestamp as since_datetime — only
         rows that changed since then are returned.
    """
    synced_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")

    rows = dao.prices.get_latest(db, instrument_ids, since=since_datetime)

    return {
        "prices": [
            {
                "instrument_id":  r["instr_id"],
                "exchange":       r["exchange"],
                "price_date":     r["price_date"],
                "price":          r["price"],
                "last_synced_at": r["last_synced_at"],
            }
            for r in rows
        ],
        "synced_at": synced_at,
    }


@router.get("/trading-calendar")
def trading_calendar(
    year: int = Query(..., description="Calendar year e.g. 2026"),
    db: Database = Depends(get_db),
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
