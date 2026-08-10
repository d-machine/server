"""Tests for scheduled jobs: cancellation of declined subs and underpaid reminders."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import text


def _insert_underpaid(auth_engine, person_id: int, days_ago: int, last_reminder_at=None):
    since = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")
    with auth_engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO underpaid_users
                    (person_id, required_price, underpaid_since, last_reminder_at)
                VALUES (:pid, 1200, :since, :lr)
            """),
            {"pid": person_id, "since": since, "lr": last_reminder_at},
        )


def test_expired_trial_subscription_loses_access(auth_engine, client, bearer, person_id):
    """A TRIAL subscription past its expires_at should not satisfy require_active_subscription."""
    # Backdate the TRIAL subscription's expires_at so it is already expired
    with auth_engine.begin() as conn:
        conn.execute(
            text("""
                UPDATE subscriptions
                SET expires_at=datetime('now', '-1 hour')
                WHERE person_id=:pid AND status='TRIAL'
            """),
            {"pid": person_id},
        )

    r = client.get("/prices/latest", headers=bearer)
    assert r.status_code == 403


def _query_reminder_candidates(auth_engine):
    """Run the same WHERE clause the reminder job uses; return matching rows."""
    with auth_engine.connect() as conn:
        return conn.execute(text("""
            SELECT u.person_id FROM underpaid_users u
            WHERE date(u.underpaid_since, '+23 days') <= date('now')
              AND date(u.underpaid_since, '+30 days') >= date('now')
              AND (u.last_reminder_at IS NULL
                   OR datetime(u.last_reminder_at, '+24 hours') <= datetime('now'))
        """)).fetchall()


def test_underpaid_reminder_sent_when_7_days_left(auth_engine, client, bearer, person_id):
    """Row appears in reminder query when underpaid_since is 24 days ago (6 days left)."""
    _insert_underpaid(auth_engine, person_id, days_ago=24)
    rows = _query_reminder_candidates(auth_engine)
    assert any(r[0] == person_id for r in rows)


def test_underpaid_reminder_skipped_if_too_early(auth_engine, client, bearer, person_id):
    """Row does NOT appear when underpaid_since is only 10 days ago (>7 days left)."""
    _insert_underpaid(auth_engine, person_id, days_ago=10)
    rows = _query_reminder_candidates(auth_engine)
    assert not any(r[0] == person_id for r in rows)


def test_underpaid_reminder_skipped_if_recent_reminder(auth_engine, client, bearer, person_id):
    """Row excluded when last_reminder_at was less than 24h ago."""
    recent = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
    _insert_underpaid(auth_engine, person_id, days_ago=24, last_reminder_at=recent)
    rows = _query_reminder_candidates(auth_engine)
    assert not any(r[0] == person_id for r in rows)
