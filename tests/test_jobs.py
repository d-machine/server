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


def test_cancel_expired_declined_subscriptions(auth_engine, client, bearer, person_id, registered_user, admin_headers):
    """DECLINED subscription past its cancel_at should be set to CANCELLED by the job SQL."""
    from io import BytesIO
    import json

    persons_payload = json.dumps([{"person_id": person_id, "amount": 1000}])
    r = client.post(
        "/subscriptions/submit",
        data={"persons": persons_payload},
        files={"screenshot": ("t.png", BytesIO(b"x"), "image/png")},
        headers=bearer,
    )
    sub_id = r.json()["created"][0]["subscription_id"]

    r = client.post(
        f"/subscriptions/admin/{sub_id}/decline",
        data={"reason": "Bad screenshot"},
        headers=admin_headers,
    )
    assert r.status_code == 200

    # Backdate cancel_at so the job condition fires
    with auth_engine.begin() as conn:
        conn.execute(
            text("UPDATE subscriptions SET cancel_at=datetime('now', '-1 hour') WHERE subscription_id=:sid"),
            {"sid": sub_id},
        )

    # Run the same SQL the job runs, directly on the test DB
    with auth_engine.begin() as conn:
        conn.execute(text("""
            UPDATE subscriptions SET status='CANCELLED'
            WHERE status='DECLINED' AND cancel_at IS NOT NULL AND cancel_at <= datetime('now')
        """))

    r = client.get("/subscriptions/status", headers=bearer)
    persons = r.json()["persons"]
    assert any(p["status"] == "CANCELLED" for p in persons)


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
