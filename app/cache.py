"""
In-memory price cache.

Prices are static within a trading day — once the bhavcopy sync runs,
today's prices don't change. So we cache by date. A new day = new cache key,
old entries are auto-evicted.

Structure:
    _cache = {
        "2026-03-29": {
            12345: 2456.00,   # instr_id -> price (REAL rupees)
            ...
        }
    }
"""

from datetime import date
from typing import Dict, Optional

from arthdesk_db import Database
from arthdesk_instruments import dao

_cache: Dict[str, Dict[int, float]] = {}


def get_prices(trade_date: date) -> Optional[Dict[int, float]]:
    """Return cached prices for a date, or None if not cached."""
    return _cache.get(trade_date.isoformat())


def set_prices(trade_date: date, prices: Dict[int, float]) -> None:
    """Store prices for a date. Evicts all other dates."""
    key = trade_date.isoformat()
    _cache.clear()   # only today's data is useful — clear stale days
    _cache[key] = prices


def invalidate() -> None:
    """Clear the entire cache — called before a fresh sync writes new prices."""
    _cache.clear()


def get_single(instr_id: int, trade_date: date) -> Optional[float]:
    """Return cached price for a single instr_id on a given date."""
    day = _cache.get(trade_date.isoformat())
    if day is None:
        return None
    return day.get(instr_id)


def warm_cache(db: Database, trade_date: Optional[date] = None) -> None:
    """Load the full instr_id -> price snapshot for a date (default: today)
    from latest_prices and populate the in-memory cache. Called at app
    startup and, manually, via POST /admin/warm-price-cache after a sync."""
    if trade_date is None:
        trade_date = date.today()
    rows = dao.prices.get_all_latest_for_date(db, trade_date.isoformat())
    set_prices(trade_date, {r["instr_id"]: r["price"] for r in rows})
