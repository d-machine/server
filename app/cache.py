"""
In-memory price cache.

Prices are static within a trading day — once the EOD cron runs at 18:30 IST,
today's prices don't change. So we cache by date. A new day = new cache key,
old entries are auto-evicted.

Structure:
    _cache = {
        "2026-03-29": {
            12345: 245600,   # instrument_id -> close_price_paise
            ...
        }
    }
"""

from datetime import date
from typing import Dict, Optional

_cache: Dict[str, Dict[int, int]] = {}


def get_prices(trade_date: date) -> Optional[Dict[int, int]]:
    """Return cached prices for a date, or None if not cached."""
    return _cache.get(trade_date.isoformat())


def set_prices(trade_date: date, prices: Dict[int, int]) -> None:
    """Store prices for a date. Evicts all other dates."""
    key = trade_date.isoformat()
    _cache.clear()   # only today's data is useful — clear stale days
    _cache[key] = prices


def invalidate() -> None:
    """Clear the entire cache — called before cron writes new prices."""
    _cache.clear()


def get_single(instrument_id: int, trade_date: date) -> Optional[int]:
    """Return cached price for a single instrument_id on a given date."""
    day = _cache.get(trade_date.isoformat())
    if day is None:
        return None
    return day.get(instrument_id)
