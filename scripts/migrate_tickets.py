"""
One-time migration: move PENDING_APPROVAL and DECLINED rows from the old
subscriptions table into the new tickets + ticket_persons tables.

Run once after deploying the new schema:
    python -m scripts.migrate_tickets
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.auth_db import AuthSessionLocal
from sqlalchemy import text


def migrate():
    db = AuthSessionLocal()
    try:
        # Fetch rows that are tickets (not clean subscription state)
        rows = db.execute(text("""
            SELECT subscription_id, user_id, person_id, paid_price,
                   screenshot_path, status, decline_reason, submitted_at
            FROM subscriptions
            WHERE status IN ('PENDING_APPROVAL', 'DECLINED')
        """)).fetchall()

        print(f"Found {len(rows)} ticket-like rows to migrate.")

        for r in rows:
            sub_id, user_id, person_id, paid_price, screenshot_path, status, decline_reason, submitted_at = r

            ticket_status = "PENDING" if status == "PENDING_APPROVAL" else "DECLINED"
            resolved_at = None if ticket_status == "PENDING" else submitted_at  # best guess

            result = db.execute(text("""
                INSERT INTO tickets (user_id, screenshot_path, status, decline_reason, submitted_at, resolved_at)
                VALUES (:uid, :ss, :status, :reason, :submitted, :resolved)
            """), {
                "uid": user_id,
                "ss": screenshot_path,
                "status": ticket_status,
                "reason": decline_reason,
                "submitted": submitted_at,
                "resolved": resolved_at,
            })
            ticket_id = result.lastrowid

            if person_id:
                db.execute(text("""
                    INSERT INTO ticket_persons (ticket_id, person_id, amount)
                    VALUES (:tid, :pid, :amount)
                """), {
                    "tid": ticket_id,
                    "pid": person_id,
                    "amount": paid_price or 1000,
                })

            # Remove migrated row from subscriptions
            db.execute(text("DELETE FROM subscriptions WHERE subscription_id=:sid"), {"sid": sub_id})

        db.commit()
        print("Migration complete.")

    except Exception as e:
        db.rollback()
        print(f"Migration failed: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
