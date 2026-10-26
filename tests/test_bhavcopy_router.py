"""
Tests for app/routers/bhavcopy.py's catchup_range() -- the pure date-range
decision used by the daily bhavcopy catch-up cron (app/main.py's
_bhavcopy_daily_catchup). No DB/network -- this is the logic that decides
*what* to sync, kept separate from the actual download/parse I/O so it's
testable without mocking either.
"""
from datetime import date

from app.routers.bhavcopy import catchup_range


class TestCatchupRange:

    def test_already_caught_up_returns_empty(self):
        yesterday = date(2026, 10, 25)
        assert catchup_range("2026-10-25", yesterday) == []

    def test_one_day_behind(self):
        yesterday = date(2026, 10, 25)
        assert catchup_range("2026-10-24", yesterday) == [date(2026, 10, 25)]

    def test_multi_day_gap_fills_entire_range(self):
        yesterday = date(2026, 10, 26)
        result = catchup_range("2026-10-22", yesterday)
        assert result == [date(2026, 10, 23), date(2026, 10, 24), date(2026, 10, 25), date(2026, 10, 26)]

    def test_never_synced_tries_only_yesterday(self):
        """None does NOT trigger a historical backfill -- that stays a manual
        /admin/bhavcopy/download job. A never-synced source just attempts
        yesterday on this run and picks up from there afterwards."""
        yesterday = date(2026, 10, 25)
        assert catchup_range(None, yesterday) == [date(2026, 10, 25)]

    def test_last_synced_after_yesterday_returns_empty(self):
        """Shouldn't happen in practice, but a future-dated last_synced must
        never produce a negative-length range."""
        yesterday = date(2026, 10, 25)
        assert catchup_range("2026-10-26", yesterday) == []
