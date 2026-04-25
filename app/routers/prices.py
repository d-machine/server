from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.database import get_db
from app import cache

router = APIRouter()


@router.get("/latest")
def latest_prices(
    isins: List[str] = Query(..., description="List of ISINs the client holds"),
):
    """
    Return today's latest price (OHLC) for each ISIN.
    Served from in-memory cache — no DB hit after first request of the day.
    """
    today = date.today()
    cached = cache.get_prices(today)

    if cached is not None:
        result = {isin: cached.get(isin) for isin in isins if isin in cached}
        return {"date": today.isoformat(), "prices": result}

    return {"date": today.isoformat(), "prices": {}, "cache_miss": True}


@router.get("/sync")
def sync_prices(
    isins: List[str] = Query(..., description="List of ISINs the client holds"),
    since_date: Optional[str] = Query(
        None,
        description="[Legacy] Return EOD prices after this YYYY-MM-DD date.",
    ),
    since_datetime: Optional[str] = Query(
        None,
        description="Return latest_prices rows where last_synced_at > this ISO datetime. "
                    "Use this for intraday incremental sync. "
                    "Format: YYYY-MM-DDTHH:MM:SS  e.g. 2026-04-16T10:30:00",
    ),
    db: Session = Depends(get_db),
):
    """
    Incremental price sync.

    Two modes:
    - **since_datetime** (preferred): returns latest_prices rows (OHLC + last_synced_at)
      for the client's ISINs that changed since the given datetime.  Used for
      intraday portfolio valuation.
    - **since_date** (legacy / historical): returns all daily_prices rows after
      the given date.  Used for charting / capital-gains history.

    Typical client flow:
      1. On first launch: call with no `since_datetime` to get all latest prices.
      2. Store the `synced_at` timestamp returned in the response.
      3. On subsequent calls: pass that timestamp as `since_datetime`.
    """
    if not isins:
        return {"prices": [], "synced_at": None}

    named_placeholders = ",".join(f":isin_{i}" for i in range(len(isins)))
    params: dict = {f"isin_{i}": isin for i, isin in enumerate(isins)}

    from datetime import datetime
    synced_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")

    # ── Latest-price sync (default + incremental) ─────────────────────────────
    # No since_datetime  → return all current latest_prices for these ISINs.
    # With since_datetime → return only rows that changed since that timestamp.
    if since_date is None:
        date_filter = ""
        if since_datetime is not None:
            params["since_dt"] = since_datetime
            date_filter = "AND lp.last_synced_at > :since_dt"

        rows = db.execute(
            text(f"""
                SELECT
                    i.isin,
                    lp.price_date,
                    lp.open_price_paise,
                    lp.high_price_paise,
                    lp.low_price_paise,
                    lp.close_price_paise,
                    lp.last_synced_at
                FROM latest_prices lp
                JOIN instruments i ON lp.instrument_id = i.instrument_id
                WHERE i.isin IN ({named_placeholders})
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
                i.isin,
                dp.trade_date  AS price_date,
                dp.open_price_paise,
                dp.high_price_paise,
                dp.low_price_paise,
                dp.close_price_paise
            FROM daily_prices dp
            JOIN instruments i ON dp.instrument_id = i.instrument_id
            WHERE i.isin IN ({named_placeholders})
              AND dp.trade_date > :since_date
            ORDER BY i.isin, dp.trade_date
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
            SELECT holiday_date, description
            FROM trading_calendar
            WHERE holiday_date LIKE :year_prefix
            ORDER BY holiday_date
        """),
        {"year_prefix": f"{year}-%"},
    ).mappings().all()

    return {"holidays": [dict(r) for r in rows]}
