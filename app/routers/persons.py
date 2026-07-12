import json
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth_db import get_auth_db
from app.crypto import encrypt_for_desktop
from app.routers.deps import get_current_user
from app.routers.subscriptions import _fy_bounds

router = APIRouter()


class PersonCreate(BaseModel):
    pan_hash: str
    masked_pan: str
    display_name: str


def _required_price(gains: float | None) -> int:
    """Same formula as pricing page: ₹1000 base + 0.02% of gains above ₹10L, capped at ₹10000."""
    if not gains:
        return 1000
    return min(10000, round(1000 + 0.0002 * max(0, gains - 1_000_000)))


def _derive_status(paid_this_fy: int, required: int, trial_expires: str | None) -> str:
    """Derive effective subscription status from FY payments."""
    if paid_this_fy >= required:
        return "ACTIVE"
    if paid_this_fy > 0:
        return "UNDERPAID"
    # No payments yet — check if still in trial window
    if trial_expires:
        try:
            exp = datetime.fromisoformat(trial_expires.replace("Z", "+00:00"))
            if exp > datetime.now(timezone.utc):
                return "TRIAL"
        except ValueError:
            pass
    return "EXPIRED"


def _get_persons_rows(user_id: int, auth_db: Session) -> list[dict]:
    fy_start, fy_end = _fy_bounds()

    rows = auth_db.execute(
        text("""
            SELECT
                p.person_id,
                p.display_name,
                p.masked_pan,
                p.pan_hash,
                -- FY paid amount from approved tickets
                COALESCE((
                    SELECT SUM(COALESCE(tp.approved_amount, tp.amount))
                    FROM tickets t
                    JOIN ticket_persons tp ON tp.ticket_id = t.ticket_id
                    WHERE tp.person_id = p.person_id
                      AND t.status = 'APPROVED'
                      AND date(t.resolved_at) BETWEEN :fy_start AND :fy_end
                ), 0) AS paid_this_fy,
                -- trial expiry from earliest subscription (TRIAL row created at registration)
                (SELECT expires_at FROM subscriptions
                 WHERE person_id = p.person_id AND status = 'TRIAL'
                 ORDER BY created_at ASC LIMIT 1) AS trial_expires_at
            FROM persons p
            WHERE p.user_id = :uid
            ORDER BY p.created_at
        """),
        {"uid": user_id, "fy_start": fy_start, "fy_end": fy_end},
    ).fetchall()

    required = _required_price(None)  # default ₹1000 until gains data is available
    return [
        {
            "person_id":            r[0],
            "display_name":         r[1],
            "masked_pan":           r[2],
            "pan_hash":             r[3],
            "subscription_status":  _derive_status(r[4], required, r[5]),
            "paid_price":           r[4],
            "expires_at":           r[5],
        }
        for r in rows
    ]


@router.get("")
def list_persons(
    user: dict = Depends(get_current_user),
    auth_db: Session = Depends(get_auth_db),
):
    """Website-safe endpoint — returns persons without masked_pan."""
    persons = _get_persons_rows(user["user_id"], auth_db)
    return [
        {k: v for k, v in p.items() if k not in ("masked_pan", "pan_hash")}
        for p in persons
    ]


@router.get("/secure")
def list_persons_secure(
    x_public_key: str = Header(..., alias="X-Public-Key"),
    user: dict = Depends(get_current_user),
    auth_db: Session = Depends(get_auth_db),
):
    """Desktop endpoint — returns persons including masked_pan, encrypted with desktop's RSA public key."""
    persons = _get_persons_rows(user["user_id"], auth_db)
    try:
        encrypted = encrypt_for_desktop(json.dumps(persons), x_public_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"data": encrypted}


@router.post("", status_code=201)
def create_person(
    body: PersonCreate,
    user: dict = Depends(get_current_user),
    auth_db: Session = Depends(get_auth_db),
):
    existing = auth_db.execute(
        text("SELECT person_id FROM persons WHERE user_id = :uid AND pan_hash = :ph"),
        {"uid": user["user_id"], "ph": body.pan_hash},
    ).fetchone()
    if existing:
        raise HTTPException(status_code=409, detail="Person with this PAN already registered")

    result = auth_db.execute(
        text("""
            INSERT INTO persons (user_id, pan_hash, masked_pan, display_name)
            VALUES (:uid, :ph, :mp, :dn)
        """),
        {
            "uid": user["user_id"],
            "ph":  body.pan_hash,
            "mp":  body.masked_pan,
            "dn":  body.display_name,
        },
    )
    person_id = result.lastrowid
    now = datetime.now(timezone.utc)
    auth_db.execute(
        text("""
            INSERT INTO subscriptions (user_id, person_id, plan, status, paid_price, starts_at, expires_at)
            VALUES (:uid, :pid, 'YEAR', 'TRIAL', 0, :starts, :expires)
        """),
        {
            "uid":    user["user_id"],
            "pid":    person_id,
            "starts": now.isoformat(),
            "expires": (now + timedelta(days=30)).isoformat(),
        },
    )
    auth_db.commit()
    return {"person_id": person_id, "display_name": body.display_name}


@router.delete("/{person_id}", status_code=204)
def delete_person(
    person_id: int,
    user: dict = Depends(get_current_user),
    auth_db: Session = Depends(get_auth_db),
):
    row = auth_db.execute(
        text("SELECT person_id FROM persons WHERE person_id = :pid AND user_id = :uid"),
        {"pid": person_id, "uid": user["user_id"]},
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Person not found")

    active_sub = auth_db.execute(
        text("""
            SELECT subscription_id FROM subscriptions
            WHERE person_id = :pid AND status = 'ACTIVE'
            AND (expires_at IS NULL OR expires_at > datetime('now'))
            LIMIT 1
        """),
        {"pid": person_id},
    ).fetchone()
    if active_sub:
        raise HTTPException(status_code=409, detail="Cannot delete person with active subscription")

    auth_db.execute(text("DELETE FROM persons WHERE person_id = :pid"), {"pid": person_id})
    auth_db.commit()
