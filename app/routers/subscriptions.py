import os
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth_db import get_auth_db
from app.routers.deps import get_current_user, require_admin

router = APIRouter()

LOCAL_DEV = os.getenv("LOCAL_DEV", "").lower() in ("1", "true", "yes")


def _fy_bounds() -> tuple[str, str]:
    """Return (start, end) ISO date strings for the current Apr–Mar financial year."""
    today = date.today()
    fy_start_year = today.year if today.month >= 4 else today.year - 1
    return f"{fy_start_year}-04-01", f"{fy_start_year + 1}-03-31"


# ---------------------------------------------------------------------------
# User endpoints
# ---------------------------------------------------------------------------

@router.get("/status")
def subscription_status(
    user: dict = Depends(get_current_user),
    auth_db: Session = Depends(get_auth_db),
):
    """Returns current subscription status for all persons of the logged-in user.
    One row per person (latest subscription). paid_this_fy = sum approved this FY."""
    fy_start, fy_end = _fy_bounds()

    rows = auth_db.execute(text("""
        SELECT s.subscription_id, s.person_id, p.display_name,
               s.status, s.expires_at, s.paid_price,
               COALESCE((
                   SELECT SUM(COALESCE(tp.approved_amount, tp.amount))
                   FROM tickets t
                   JOIN ticket_persons tp ON tp.ticket_id = t.ticket_id
                   WHERE tp.person_id = s.person_id
                     AND t.status = 'APPROVED'
                     AND date(t.resolved_at) BETWEEN :fy_start AND :fy_end
               ), 0) AS paid_this_fy
        FROM subscriptions s
        JOIN persons p ON p.person_id = s.person_id
        WHERE s.user_id = :uid
        ORDER BY s.created_at DESC
    """), {"uid": user["user_id"], "fy_start": fy_start, "fy_end": fy_end}).fetchall()

    if not rows:
        return {"has_subscription": False, "persons": []}

    # One entry per person_id — first row wins (latest subscription)
    seen: set[int] = set()
    persons = []
    for r in rows:
        pid = r[1]
        if pid in seen:
            continue
        seen.add(pid)
        persons.append({
            "subscription_id": r[0],
            "person_id":       pid,
            "display_name":    r[2],
            "status":          r[3],
            "expires_at":      r[4],
            "paid_price":      r[5],
            "paid_this_fy":    r[6],
        })

    return {"has_subscription": True, "persons": persons}


@router.get("/history")
def payment_history(
    user: dict = Depends(get_current_user),
    auth_db: Session = Depends(get_auth_db),
):
    """Returns all APPROVED tickets for the logged-in user, newest first."""
    rows = auth_db.execute(text("""
        SELECT t.ticket_id, t.submitted_at, t.resolved_at,
               tp.person_id, p.display_name,
               COALESCE(tp.approved_amount, tp.amount) AS amount
        FROM tickets t
        JOIN ticket_persons tp ON tp.ticket_id = t.ticket_id
        JOIN persons p ON p.person_id = tp.person_id
        WHERE t.user_id = :uid AND t.status = 'APPROVED'
        ORDER BY t.resolved_at DESC, t.ticket_id DESC
    """), {"uid": user["user_id"]}).fetchall()

    # Group by ticket
    tickets: dict[int, dict] = {}
    order: list[int] = []
    for r in rows:
        tid = r[0]
        if tid not in tickets:
            tickets[tid] = {
                "ticket_id":    tid,
                "submitted_at": r[1],
                "resolved_at":  r[2],
                "persons":      [],
                "total":        0,
            }
            order.append(tid)
        tickets[tid]["persons"].append({
            "person_id":    r[3],
            "display_name": r[4],
            "amount":       r[5],
        })
        tickets[tid]["total"] += r[5] or 0

    return [tickets[tid] for tid in order]


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------

@router.get("/admin/users")
def admin_list_users(
    _: None = Depends(require_admin),
    auth_db: Session = Depends(get_auth_db),
):
    rows = auth_db.execute(text("""
        SELECT u.user_id, u.email, u.name,
               COUNT(DISTINCT p.person_id) AS person_count,
               COALESCE(SUM(s.paid_price), 0) AS total_paid
        FROM users u
        LEFT JOIN persons p ON p.user_id = u.user_id
        LEFT JOIN subscriptions s ON s.person_id = p.person_id AND s.status = 'ACTIVE'
        GROUP BY u.user_id
        ORDER BY u.created_at DESC
    """)).fetchall()
    return [
        {
            "user_id":      r[0],
            "email":        r[1],
            "name":         r[2],
            "person_count": r[3],
            "total_paid":   r[4],
        }
        for r in rows
    ]


@router.get("/admin/users/{user_id}")
def admin_get_user(
    user_id: int,
    _: None = Depends(require_admin),
    auth_db: Session = Depends(get_auth_db),
):
    user_row = auth_db.execute(
        text("SELECT user_id, email, name, created_at FROM users WHERE user_id=:uid"),
        {"uid": user_id},
    ).fetchone()
    if not user_row:
        raise HTTPException(status_code=404, detail="User not found")

    persons = auth_db.execute(text("""
        SELECT p.person_id, p.display_name,
               s.status, s.expires_at, s.paid_price,
               u2.required_price
        FROM persons p
        LEFT JOIN subscriptions s ON s.person_id = p.person_id
        LEFT JOIN underpaid_users u2 ON u2.person_id = p.person_id
        WHERE p.user_id = :uid
        ORDER BY p.created_at DESC
    """), {"uid": user_id}).fetchall()

    return {
        "user_id":    user_row[0],
        "email":      user_row[1],
        "name":       user_row[2],
        "created_at": user_row[3],
        "persons": [
            {
                "person_id":      r[0],
                "display_name":   r[1],
                "status":         r[2] or "NONE",
                "expires_at":     r[3],
                "paid_price":     r[4],
                "required_price": r[5],
            }
            for r in persons
        ],
    }


@router.get("/admin/persons")
def admin_list_persons(
    status: str | None = None,
    _: None = Depends(require_admin),
    auth_db: Session = Depends(get_auth_db),
):
    """
    Returns all persons with their subscription state.
    status filter: ACTIVE | EXPIRED | UNDERPAID | CANCELLED | NONE
    """
    base_query = """
        SELECT p.person_id, p.display_name,
               u.user_id, u.email,
               s.status, s.expires_at, s.paid_price,
               up.required_price
        FROM persons p
        JOIN users u ON u.user_id = p.user_id
        LEFT JOIN subscriptions s ON s.person_id = p.person_id
        LEFT JOIN underpaid_users up ON up.person_id = p.person_id
    """
    params: dict = {}
    if status == "NONE":
        base_query += " WHERE s.subscription_id IS NULL"
    elif status == "UNDERPAID":
        base_query += " WHERE up.person_id IS NOT NULL"
    elif status:
        base_query += " WHERE s.status = :status"
        params["status"] = status
    base_query += " ORDER BY p.created_at DESC"

    rows = auth_db.execute(text(base_query), params).fetchall()
    return [
        {
            "person_id":      r[0],
            "display_name":   r[1],
            "user_id":        r[2],
            "user_email":     r[3],
            "status":         r[4] or "NONE",
            "expires_at":     r[5],
            "paid_price":     r[6],
            "required_price": r[7],
        }
        for r in rows
    ]


@router.post("/admin/persons/{person_id}/block")
def admin_block_person(
    person_id: int,
    _: None = Depends(require_admin),
    auth_db: Session = Depends(get_auth_db),
):
    prow = auth_db.execute(
        text("SELECT person_id FROM persons WHERE person_id=:pid"), {"pid": person_id}
    ).fetchone()
    if not prow:
        raise HTTPException(status_code=404, detail="Person not found")

    auth_db.execute(
        text("UPDATE subscriptions SET status='CANCELLED' WHERE person_id=:pid"),
        {"pid": person_id},
    )
    auth_db.commit()
    return {"ok": True, "status": "CANCELLED"}


@router.post("/admin/persons/{person_id}/unblock")
def admin_unblock_person(
    person_id: int,
    _: None = Depends(require_admin),
    auth_db: Session = Depends(get_auth_db),
):
    prow = auth_db.execute(
        text("SELECT person_id FROM persons WHERE person_id=:pid"), {"pid": person_id}
    ).fetchone()
    if not prow:
        raise HTTPException(status_code=404, detail="Person not found")

    srow = auth_db.execute(
        text("SELECT subscription_id FROM subscriptions WHERE person_id=:pid LIMIT 1"),
        {"pid": person_id},
    ).fetchone()
    if not srow:
        raise HTTPException(status_code=400, detail="No subscription row to unblock — approve a ticket first")

    auth_db.execute(
        text("UPDATE subscriptions SET status='ACTIVE' WHERE person_id=:pid"),
        {"pid": person_id},
    )
    auth_db.commit()
    return {"ok": True, "status": "ACTIVE"}
