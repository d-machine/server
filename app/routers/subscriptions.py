import json
import os
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from google.cloud import storage as _gcs
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth_db import get_auth_db
from app.routers.deps import get_current_user, require_admin

router = APIRouter()

GCS_BUCKET       = os.getenv("GCS_BHAVCOPY_BUCKET", "arthdesk-bhavcopy")
SMTP_HOST        = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT        = int(os.getenv("SMTP_PORT", 587))
SMTP_USER        = os.getenv("SMTP_USER", "sumitshark13@gmail.com")
SMTP_PASS        = os.getenv("SMTP_PASS", "")
FROM_EMAIL       = os.getenv("FROM_EMAIL", "sumitshark13@gmail.com")
BASE_URL         = os.getenv("BASE_URL", "https://arthdeskapi.ashokitservices.com")

PLAN_DAYS = {"MONTH": 30, "YEAR": 365}

_gcs_client: _gcs.Client | None = None


def _gcs_bucket() -> _gcs.Bucket:
    global _gcs_client
    if _gcs_client is None:
        _gcs_client = _gcs.Client()
    return _gcs_client.bucket(GCS_BUCKET)


def _upload_screenshot(subscription_id: int, file: UploadFile) -> str:
    ext = (file.filename or "screenshot").rsplit(".", 1)[-1].lower()
    object_name = f"screenshots/{subscription_id}_{file.filename}"
    blob = _gcs_bucket().blob(object_name)
    blob.upload_from_file(file.file, content_type=file.content_type or f"image/{ext}")
    return object_name


def _signed_url(object_name: str) -> str:
    blob = _gcs_bucket().blob(object_name)
    return blob.generate_signed_url(expiration=timedelta(minutes=15), method="GET")


def _send_email(to: str, subject: str, body: str):
    if not SMTP_PASS:
        return
    msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"]    = FROM_EMAIL
    msg["To"]      = to
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)


def _user_email(auth_db: Session, user_id: int) -> str:
    row = auth_db.execute(
        text("SELECT email FROM users WHERE user_id=:uid"), {"uid": user_id}
    ).fetchone()
    return row[0] if row else ""


# ---------------------------------------------------------------------------
# User endpoints
# ---------------------------------------------------------------------------

@router.post("/submit")
async def submit_subscription(
    persons: str = Form(...),  # JSON: [{"person_id": 1, "amount": 1000}, ...]
    screenshot: UploadFile = File(...),
    user: dict = Depends(get_current_user),
    auth_db: Session = Depends(get_auth_db),
):
    """
    Submit a payment screenshot covering one or more registered persons.
    Creates one PENDING_APPROVAL subscription row per person.
    """
    try:
        person_entries = json.loads(persons)
        if not isinstance(person_entries, list) or not person_entries:
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="'persons' must be a non-empty JSON array")

    screenshot_bytes = await screenshot.read()

    created = []
    for entry in person_entries:
        person_id = entry.get("person_id")
        amount    = entry.get("amount", 1000)

        # Verify the person belongs to this user
        prow = auth_db.execute(
            text("SELECT person_id FROM persons WHERE person_id=:pid AND user_id=:uid"),
            {"pid": person_id, "uid": user["user_id"]},
        ).fetchone()
        if not prow:
            raise HTTPException(status_code=400, detail=f"Person {person_id} not found")

        # Block if already pending/active for this person
        existing = auth_db.execute(
            text("""
                SELECT subscription_id, status FROM subscriptions
                WHERE person_id=:pid AND status IN ('PENDING_APPROVAL','ACTIVE')
                LIMIT 1
            """),
            {"pid": person_id},
        ).fetchone()
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"Person {person_id} already has a subscription with status '{existing[1]}'"
            )

        result = auth_db.execute(
            text("""
                INSERT INTO subscriptions (user_id, person_id, plan, status, paid_price)
                VALUES (:uid, :pid, 'YEAR', 'PENDING_APPROVAL', :amount)
            """),
            {"uid": user["user_id"], "pid": person_id, "amount": amount},
        )
        auth_db.commit()
        subscription_id = result.lastrowid

        import io
        ext = (screenshot.filename or "screenshot").rsplit(".", 1)[-1].lower()
        object_name = f"screenshots/{subscription_id}_{screenshot.filename}"
        blob = _gcs_bucket().blob(object_name)
        blob.upload_from_file(io.BytesIO(screenshot_bytes), content_type=screenshot.content_type or f"image/{ext}")

        auth_db.execute(
            text("UPDATE subscriptions SET screenshot_path=:path WHERE subscription_id=:sid"),
            {"path": object_name, "sid": subscription_id},
        )
        auth_db.commit()
        created.append({"subscription_id": subscription_id, "person_id": person_id, "status": "PENDING_APPROVAL"})

    return {"created": created}


@router.get("/status")
def subscription_status(
    user: dict = Depends(get_current_user),
    auth_db: Session = Depends(get_auth_db),
):
    """Returns subscription status for all persons associated with the logged-in user."""
    rows = auth_db.execute(
        text("""
            SELECT s.subscription_id, s.person_id, p.display_name,
                   s.status, s.expires_at, s.submitted_at, s.decline_reason, s.paid_price
            FROM subscriptions s
            JOIN persons p ON p.person_id = s.person_id
            WHERE s.user_id=:uid
            ORDER BY s.created_at DESC
        """),
        {"uid": user["user_id"]},
    ).fetchall()
    if not rows:
        return {"has_subscription": False, "persons": []}
    return {
        "has_subscription": True,
        "persons": [
            {
                "subscription_id": r[0],
                "person_id":       r[1],
                "display_name":    r[2],
                "status":          r[3],
                "expires_at":      r[4],
                "submitted_at":    r[5],
                "decline_reason":  r[6],
                "paid_price":      r[7],
            }
            for r in rows
        ],
    }


@router.post("/replace-screenshot/{person_id}")
async def replace_screenshot(
    person_id: int,
    screenshot: UploadFile = File(...),
    user: dict = Depends(get_current_user),
    auth_db: Session = Depends(get_auth_db),
):
    row = auth_db.execute(
        text("""
            SELECT s.subscription_id, s.status FROM subscriptions s
            JOIN persons p ON p.person_id = s.person_id
            WHERE s.person_id=:pid AND p.user_id=:uid
            ORDER BY s.created_at DESC LIMIT 1
        """),
        {"pid": person_id, "uid": user["user_id"]},
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="No subscription found for this person")
    if row[1] != "DECLINED":
        raise HTTPException(status_code=400, detail="Can only replace screenshot on DECLINED subscriptions")

    subscription_id = row[0]
    object_name = _upload_screenshot(subscription_id, screenshot)
    auth_db.execute(
        text("""
            UPDATE subscriptions
            SET screenshot_path=:path, status='PENDING_APPROVAL',
                decline_reason=NULL, cancel_at=NULL
            WHERE subscription_id=:sid
        """),
        {"path": object_name, "sid": subscription_id},
    )
    auth_db.commit()
    return {"subscription_id": subscription_id, "status": "PENDING_APPROVAL"}


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------

@router.get("/admin")
def admin_list_subscriptions(
    status: str | None = None,
    _: None = Depends(require_admin),
    auth_db: Session = Depends(get_auth_db),
):
    query = """
        SELECT s.subscription_id, s.plan, s.status, s.expires_at, s.submitted_at,
               s.decline_reason, s.cancel_at, s.screenshot_path, s.paid_price,
               u.email, u.name,
               p.display_name AS person_name, p.masked_pan
        FROM subscriptions s
        JOIN users u ON s.user_id=u.user_id
        LEFT JOIN persons p ON s.person_id=p.person_id
    """
    params: dict = {}
    if status:
        query += " WHERE s.status=:status"
        params["status"] = status
    query += " ORDER BY s.submitted_at DESC"
    rows = auth_db.execute(text(query), params).fetchall()

    result = []
    for r in rows:
        screenshot_url = None
        if r[7]:
            try:
                screenshot_url = _signed_url(r[7])
            except Exception:
                pass
        result.append({
            "subscription_id": r[0],
            "plan":            r[1],
            "status":          r[2],
            "expires_at":      r[3],
            "submitted_at":    r[4],
            "decline_reason":  r[5],
            "cancel_at":       r[6],
            "screenshot_url":  screenshot_url,
            "paid_price":      r[8],
            "email":           r[9],
            "name":            r[10],
            "person_name":     r[11],
            "masked_pan":      r[12],
        })
    return result


@router.post("/admin/{subscription_id}/approve")
def admin_approve(
    subscription_id: int,
    _: None = Depends(require_admin),
    auth_db: Session = Depends(get_auth_db),
):
    row = auth_db.execute(
        text("SELECT user_id, plan, status FROM subscriptions WHERE subscription_id=:sid"),
        {"sid": subscription_id},
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Subscription not found")

    plan = row[1]
    days = PLAN_DAYS.get(plan, 30)
    now  = datetime.now(timezone.utc)
    expires_at = (now + timedelta(days=days)).isoformat()

    auth_db.execute(
        text("""
            UPDATE subscriptions
            SET status='ACTIVE', starts_at=:now, expires_at=:exp,
                decline_reason=NULL, cancel_at=NULL
            WHERE subscription_id=:sid
        """),
        {"now": now.isoformat(), "exp": expires_at, "sid": subscription_id},
    )
    auth_db.commit()

    email = _user_email(auth_db, row[0])
    _send_email(
        to=email,
        subject="Your Portfolio Tracker subscription is now active!",
        body=(
            f"Hi,\n\nYour {plan} subscription has been approved and is now active.\n"
            f"It will expire on {expires_at[:10]}.\n\nThank you for subscribing!"
        ),
    )
    return {"ok": True, "expires_at": expires_at}


@router.post("/admin/{subscription_id}/decline")
def admin_decline(
    subscription_id: int,
    reason: str = Form(...),
    _: None = Depends(require_admin),
    auth_db: Session = Depends(get_auth_db),
):
    row = auth_db.execute(
        text("SELECT user_id, status FROM subscriptions WHERE subscription_id=:sid"),
        {"sid": subscription_id},
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Subscription not found")

    user_id    = row[0]
    was_active = row[1] == "ACTIVE"
    cancel_at  = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat() if was_active else None

    auth_db.execute(
        text("""
            UPDATE subscriptions
            SET status='DECLINED', decline_reason=:reason, cancel_at=:cancel_at
            WHERE subscription_id=:sid
        """),
        {"reason": reason, "cancel_at": cancel_at, "sid": subscription_id},
    )
    auth_db.commit()

    email = _user_email(auth_db, user_id)
    notice = (
        "\n\nNote: If your subscription was active, it will be cancelled in 24 hours."
        if was_active else ""
    )
    _send_email(
        to=email,
        subject="Action required: Portfolio Tracker subscription",
        body=(
            f"Hi,\n\nYour payment screenshot was declined for the following reason:\n\n"
            f"{reason}\n\n"
            f"Please upload a corrected screenshot at:\n{BASE_URL}/account.html\n\n"
            f"Or reply to this email with the correct screenshot and we will upload it for you."
            f"{notice}"
        ),
    )
    return {"ok": True}


@router.post("/admin/{subscription_id}/screenshot")
async def admin_upload_screenshot(
    subscription_id: int,
    screenshot: UploadFile = File(...),
    _: None = Depends(require_admin),
    auth_db: Session = Depends(get_auth_db),
):
    row = auth_db.execute(
        text("SELECT subscription_id FROM subscriptions WHERE subscription_id=:sid"),
        {"sid": subscription_id},
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Subscription not found")

    object_name = _upload_screenshot(subscription_id, screenshot)
    auth_db.execute(
        text("""
            UPDATE subscriptions
            SET screenshot_path=:path, status='PENDING_APPROVAL',
                decline_reason=NULL, cancel_at=NULL
            WHERE subscription_id=:sid
        """),
        {"path": object_name, "sid": subscription_id},
    )
    auth_db.commit()
    return {"subscription_id": subscription_id, "status": "PENDING_APPROVAL"}
