import json

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth_db import get_auth_db
from app.crypto import encrypt_for_desktop
from app.routers.deps import get_current_user

router = APIRouter()


class PersonCreate(BaseModel):
    pan_hash: str
    masked_pan: str
    display_name: str


def _get_persons_rows(user_id: int, auth_db: Session) -> list[dict]:
    rows = auth_db.execute(
        text("""
            SELECT
                p.person_id,
                p.display_name,
                p.masked_pan,
                p.pan_hash,
                s.status        AS subscription_status,
                s.paid_price,
                s.starts_at,
                s.expires_at
            FROM persons p
            LEFT JOIN subscriptions s
                ON s.person_id = p.person_id
                AND s.status = 'ACTIVE'
                AND (s.expires_at IS NULL OR s.expires_at > datetime('now'))
            WHERE p.user_id = :uid
            ORDER BY p.created_at
        """),
        {"uid": user_id},
    ).fetchall()
    return [
        {
            "person_id":            r[0],
            "display_name":         r[1],
            "masked_pan":           r[2],
            "pan_hash":             r[3],
            "subscription_status":  r[4] or "NONE",
            "paid_price":           r[5],
            "starts_at":            r[6],
            "expires_at":           r[7],
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
    auth_db.commit()
    return {"person_id": result.lastrowid, "display_name": body.display_name}


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
