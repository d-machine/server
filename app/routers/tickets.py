import io
import json
import os
import pathlib
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth_db import get_auth_db
from app.routers.deps import get_current_user, require_admin

router = APIRouter()

SMTP_HOST  = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT  = int(os.getenv("SMTP_PORT", 587))
SMTP_USER  = os.getenv("SMTP_USER", "sumitshark13@gmail.com")
SMTP_PASS  = os.getenv("SMTP_PASS", "")
FROM_EMAIL = os.getenv("FROM_EMAIL", "sumitshark13@gmail.com")
BASE_URL   = os.getenv("BASE_URL", "https://arthdeskapi.ashokitservices.com")
LOCAL_DEV  = os.getenv("LOCAL_DEV", "").lower() in ("1", "true", "yes")

try:
    from google.cloud import storage as _gcs
    _gcs_client = None
    def _gcs_bucket():
        global _gcs_client
        GCS_BUCKET = os.getenv("GCS_BHAVCOPY_BUCKET", "arthdesk-bhavcopy")
        if _gcs_client is None:
            _gcs_client = _gcs.Client()
        return _gcs_client.bucket(GCS_BUCKET)
except ImportError:
    _gcs_bucket = None


def _upload_screenshot(ticket_id: int, file_bytes: bytes, filename: str, content_type: str) -> str:
    object_name = f"screenshots/ticket_{ticket_id}_{filename}"
    if LOCAL_DEV:
        dest = pathlib.Path("/app/data/screenshots")
        dest.mkdir(parents=True, exist_ok=True)
        (dest / f"ticket_{ticket_id}_{filename}").write_bytes(file_bytes)
        return object_name
    ext = (filename or "screenshot").rsplit(".", 1)[-1].lower()
    bucket = _gcs_bucket()
    blob = bucket.blob(object_name)
    blob.upload_from_file(io.BytesIO(file_bytes), content_type=content_type or f"image/{ext}")
    return object_name


def _screenshot_url(screenshot_path: str) -> str | None:
    if not screenshot_path:
        return None
    if LOCAL_DEV:
        filename = pathlib.Path(screenshot_path).name
        return f"{BASE_URL}/screenshots/{filename}"
    try:
        bucket = _gcs_bucket()
        blob = bucket.blob(screenshot_path)
        return blob.generate_signed_url(expiration=timedelta(minutes=15), method="GET")
    except Exception:
        return None


def _send_email(to: str, subject: str, body: str):
    if not SMTP_PASS:
        return
    msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"] = FROM_EMAIL
    msg["To"] = to
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)


def _user_email(auth_db: Session, user_id: int) -> str:
    row = auth_db.execute(
        text("SELECT email FROM users WHERE user_id=:uid"), {"uid": user_id}
    ).fetchone()
    return row[0] if row else ""


def _ticket_detail(auth_db: Session, ticket_id: int) -> dict:
    t = auth_db.execute(text("""
        SELECT t.ticket_id, t.user_id, t.screenshot_path, t.status,
               t.decline_reason, t.submitted_at, t.resolved_at,
               u.email, u.name
        FROM tickets t
        JOIN users u ON u.user_id = t.user_id
        WHERE t.ticket_id = :tid
    """), {"tid": ticket_id}).fetchone()
    if not t:
        return None

    persons = auth_db.execute(text("""
        SELECT tp.id, tp.person_id, p.display_name,
               tp.amount, tp.approved_amount, tp.notes
        FROM ticket_persons tp
        JOIN persons p ON p.person_id = tp.person_id
        WHERE tp.ticket_id = :tid
    """), {"tid": ticket_id}).fetchall()

    return {
        "ticket_id":      t[0],
        "user_id":        t[1],
        "screenshot_url": _screenshot_url(t[2]),
        "status":         t[3],
        "decline_reason": t[4],
        "submitted_at":   t[5],
        "resolved_at":    t[6],
        "user_email":     t[7],
        "user_name":      t[8],
        "persons": [
            {
                "id":              p[0],
                "person_id":       p[1],
                "display_name":    p[2],
                "amount":          p[3],
                "approved_amount": p[4],
                "notes":           p[5],
            }
            for p in persons
        ],
    }


# ---------------------------------------------------------------------------
# User endpoints
# ---------------------------------------------------------------------------

@router.post("/submit")
async def submit_ticket(
    persons: str = Form(...),
    screenshot: UploadFile = File(...),
    user: dict = Depends(get_current_user),
    auth_db: Session = Depends(get_auth_db),
):
    try:
        person_entries = json.loads(persons)
        if not isinstance(person_entries, list) or not person_entries:
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="'persons' must be a non-empty JSON array")

    # Validate persons belong to user and have no pending ticket
    for entry in person_entries:
        person_id = entry.get("person_id")
        prow = auth_db.execute(
            text("SELECT person_id FROM persons WHERE person_id=:pid AND user_id=:uid"),
            {"pid": person_id, "uid": user["user_id"]},
        ).fetchone()
        if not prow:
            raise HTTPException(status_code=400, detail=f"Person {person_id} not found")

        existing = auth_db.execute(text("""
            SELECT t.ticket_id FROM tickets t
            JOIN ticket_persons tp ON tp.ticket_id = t.ticket_id
            WHERE tp.person_id = :pid AND t.status = 'PENDING'
            LIMIT 1
        """), {"pid": person_id}).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail=f"Person {person_id} already has a pending ticket")

    screenshot_bytes = await screenshot.read()

    result = auth_db.execute(text("""
        INSERT INTO tickets (user_id, status) VALUES (:uid, 'PENDING')
    """), {"uid": user["user_id"]})
    auth_db.commit()
    ticket_id = result.lastrowid

    object_name = _upload_screenshot(ticket_id, screenshot_bytes, screenshot.filename or "screenshot.png", screenshot.content_type or "image/png")
    auth_db.execute(
        text("UPDATE tickets SET screenshot_path=:path WHERE ticket_id=:tid"),
        {"path": object_name, "tid": ticket_id},
    )

    for entry in person_entries:
        auth_db.execute(text("""
            INSERT INTO ticket_persons (ticket_id, person_id, amount)
            VALUES (:tid, :pid, :amount)
        """), {"tid": ticket_id, "pid": entry["person_id"], "amount": entry.get("amount", 1000)})

    auth_db.commit()
    return {"ticket_id": ticket_id, "status": "PENDING"}


@router.get("/my")
def my_tickets(
    user: dict = Depends(get_current_user),
    auth_db: Session = Depends(get_auth_db),
):
    rows = auth_db.execute(text("""
        SELECT t.ticket_id, t.status, t.decline_reason, t.submitted_at, t.resolved_at,
               t.screenshot_path
        FROM tickets t
        WHERE t.user_id = :uid
        ORDER BY t.submitted_at DESC
    """), {"uid": user["user_id"]}).fetchall()

    result = []
    for t in rows:
        persons = auth_db.execute(text("""
            SELECT tp.person_id, p.display_name, tp.amount, tp.approved_amount
            FROM ticket_persons tp
            JOIN persons p ON p.person_id = tp.person_id
            WHERE tp.ticket_id = :tid
        """), {"tid": t[0]}).fetchall()
        result.append({
            "ticket_id":      t[0],
            "status":         t[1],
            "decline_reason": t[2],
            "submitted_at":   t[3],
            "resolved_at":    t[4],
            "screenshot_url": _screenshot_url(t[5]),
            "persons": [{"person_id": p[0], "display_name": p[1], "amount": p[2], "approved_amount": p[3]} for p in persons],
        })
    return result


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------

@router.get("/admin")
def admin_list_tickets(
    status: str | None = None,
    _: None = Depends(require_admin),
    auth_db: Session = Depends(get_auth_db),
):
    query = """
        SELECT t.ticket_id, t.user_id, t.screenshot_path, t.status,
               t.decline_reason, t.submitted_at, t.resolved_at,
               u.email, u.name
        FROM tickets t
        JOIN users u ON u.user_id = t.user_id
    """
    params: dict = {}
    if status:
        query += " WHERE t.status = :status"
        params["status"] = status
    query += " ORDER BY t.submitted_at DESC"

    rows = auth_db.execute(text(query), params).fetchall()
    result = []
    for t in rows:
        persons = auth_db.execute(text("""
            SELECT tp.id, tp.person_id, p.display_name, tp.amount, tp.approved_amount, tp.notes
            FROM ticket_persons tp
            JOIN persons p ON p.person_id = tp.person_id
            WHERE tp.ticket_id = :tid
        """), {"tid": t[0]}).fetchall()
        result.append({
            "ticket_id":      t[0],
            "user_id":        t[1],
            "screenshot_url": _screenshot_url(t[2]),
            "status":         t[3],
            "decline_reason": t[4],
            "submitted_at":   t[5],
            "resolved_at":    t[6],
            "user_email":     t[7],
            "user_name":      t[8],
            "persons": [
                {"id": p[0], "person_id": p[1], "display_name": p[2],
                 "amount": p[3], "approved_amount": p[4], "notes": p[5]}
                for p in persons
            ],
        })
    return result


@router.get("/admin/{ticket_id}")
def admin_get_ticket(
    ticket_id: int,
    _: None = Depends(require_admin),
    auth_db: Session = Depends(get_auth_db),
):
    detail = _ticket_detail(auth_db, ticket_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return detail


class TicketPersonUpdate(BaseModel):
    approved_amount: int | None = None
    notes: str | None = None


@router.patch("/admin/{ticket_id}/persons/{tp_id}")
def admin_update_ticket_person(
    ticket_id: int,
    tp_id: int,
    body: TicketPersonUpdate,
    _: None = Depends(require_admin),
    auth_db: Session = Depends(get_auth_db),
):
    row = auth_db.execute(
        text("SELECT id FROM ticket_persons WHERE id=:id AND ticket_id=:tid"),
        {"id": tp_id, "tid": ticket_id},
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Ticket person not found")

    auth_db.execute(text("""
        UPDATE ticket_persons
        SET approved_amount = COALESCE(:amt, approved_amount),
            notes = COALESCE(:notes, notes)
        WHERE id = :id
    """), {"amt": body.approved_amount, "notes": body.notes, "id": tp_id})
    auth_db.commit()
    return {"ok": True}


@router.post("/admin/{ticket_id}/approve")
def admin_approve_ticket(
    ticket_id: int,
    _: None = Depends(require_admin),
    auth_db: Session = Depends(get_auth_db),
):
    ticket = auth_db.execute(
        text("SELECT user_id, status FROM tickets WHERE ticket_id=:tid"),
        {"tid": ticket_id},
    ).fetchone()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    if ticket[1] != "PENDING":
        raise HTTPException(status_code=400, detail=f"Ticket is already {ticket[1]}")

    persons = auth_db.execute(text("""
        SELECT tp.person_id, COALESCE(tp.approved_amount, tp.amount)
        FROM ticket_persons tp
        WHERE tp.ticket_id = :tid
    """), {"tid": ticket_id}).fetchall()

    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(days=365)).isoformat()

    for person_id, paid_price in persons:
        auth_db.execute(text("""
            INSERT INTO subscriptions (user_id, person_id, plan, status, paid_price, starts_at, expires_at)
            VALUES (:uid, :pid, 'YEAR', 'ACTIVE', :price, :now, :exp)
            ON CONFLICT(person_id) DO UPDATE SET
                plan = 'YEAR', status = 'ACTIVE', paid_price = excluded.paid_price,
                starts_at = excluded.starts_at, expires_at = excluded.expires_at
        """), {"uid": ticket[0], "pid": person_id, "price": paid_price, "now": now.isoformat(), "exp": expires_at})

    auth_db.execute(text("""
        UPDATE tickets SET status='APPROVED', resolved_at=:now WHERE ticket_id=:tid
    """), {"now": now.isoformat(), "tid": ticket_id})
    auth_db.commit()

    email = _user_email(auth_db, ticket[0])
    _send_email(
        to=email,
        subject="Your ArthaDesk subscription is now active!",
        body=(
            f"Hi,\n\nYour payment has been verified and your subscription is now active.\n"
            f"It will expire on {expires_at[:10]}.\n\nThank you!"
        ),
    )
    return {"ok": True, "expires_at": expires_at}


@router.post("/admin/{ticket_id}/decline")
def admin_decline_ticket(
    ticket_id: int,
    reason: str = Form(...),
    _: None = Depends(require_admin),
    auth_db: Session = Depends(get_auth_db),
):
    ticket = auth_db.execute(
        text("SELECT user_id, status FROM tickets WHERE ticket_id=:tid"),
        {"tid": ticket_id},
    ).fetchone()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    if ticket[1] != "PENDING":
        raise HTTPException(status_code=400, detail=f"Ticket is already {ticket[1]}")

    now = datetime.now(timezone.utc).isoformat()
    auth_db.execute(text("""
        UPDATE tickets SET status='DECLINED', decline_reason=:reason, resolved_at=:now
        WHERE ticket_id=:tid
    """), {"reason": reason, "now": now, "tid": ticket_id})
    auth_db.commit()

    email = _user_email(auth_db, ticket[0])
    _send_email(
        to=email,
        subject="Action required: ArthaDesk payment screenshot",
        body=(
            f"Hi,\n\nYour payment screenshot was declined for the following reason:\n\n"
            f"{reason}\n\n"
            f"Please upload a corrected screenshot at:\n{BASE_URL}/subscribe.html\n\n"
            f"Or reply to this email with the correct screenshot."
        ),
    )
    return {"ok": True}


@router.post("/admin/create")
async def admin_create_ticket(
    user_id: int = Form(...),
    persons: str = Form(...),
    screenshot: UploadFile = File(None),
    _: None = Depends(require_admin),
    auth_db: Session = Depends(get_auth_db),
):
    try:
        person_entries = json.loads(persons)
        if not isinstance(person_entries, list) or not person_entries:
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="'persons' must be a non-empty JSON array")

    user_row = auth_db.execute(
        text("SELECT user_id FROM users WHERE user_id=:uid"), {"uid": user_id}
    ).fetchone()
    if not user_row:
        raise HTTPException(status_code=404, detail="User not found")

    for entry in person_entries:
        person_id = entry.get("person_id")
        prow = auth_db.execute(
            text("SELECT person_id FROM persons WHERE person_id=:pid AND user_id=:uid"),
            {"pid": person_id, "uid": user_id},
        ).fetchone()
        if not prow:
            raise HTTPException(status_code=400, detail=f"Person {person_id} not found under user {user_id}")

        existing = auth_db.execute(text("""
            SELECT t.ticket_id FROM tickets t
            JOIN ticket_persons tp ON tp.ticket_id = t.ticket_id
            WHERE tp.person_id = :pid AND t.status = 'PENDING'
            LIMIT 1
        """), {"pid": person_id}).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail=f"Person {person_id} already has a pending ticket")

    result = auth_db.execute(
        text("INSERT INTO tickets (user_id, status) VALUES (:uid, 'PENDING')"),
        {"uid": user_id},
    )
    auth_db.commit()
    ticket_id = result.lastrowid

    if screenshot and screenshot.filename:
        screenshot_bytes = await screenshot.read()
        object_name = _upload_screenshot(ticket_id, screenshot_bytes, screenshot.filename, screenshot.content_type or "image/png")
        auth_db.execute(
            text("UPDATE tickets SET screenshot_path=:path WHERE ticket_id=:tid"),
            {"path": object_name, "tid": ticket_id},
        )

    for entry in person_entries:
        auth_db.execute(text("""
            INSERT INTO ticket_persons (ticket_id, person_id, amount)
            VALUES (:tid, :pid, :amount)
        """), {"tid": ticket_id, "pid": entry["person_id"], "amount": entry.get("amount", 1000)})

    auth_db.commit()
    return {"ticket_id": ticket_id, "status": "PENDING"}
