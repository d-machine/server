import base64
import os
import re
from datetime import datetime, timezone

from fastapi import BackgroundTasks, Depends, Header, HTTPException, Request
from jose import JWTError, jwt
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.auth_db import get_auth_db

JWT_SECRET = os.getenv("JWT_SECRET", "change-me-in-production-use-32-byte-hex")
JWT_ALGORITHM = "HS256"

# Salt prefix and encoding constants for X-App-Metrics header
_METRICS_SALT = "xkpm"
_DIGIT_TO_LETTER = {str(i): chr(ord('a') + i) for i in range(10)}
_LETTER_TO_DIGIT = {v: k for k, v in _DIGIT_TO_LETTER.items()}


def _decode_metrics_segment(segment: str) -> str:
    return "".join(_LETTER_TO_DIGIT.get(c, c) for c in segment)


def _parse_metrics_header(value: str) -> tuple[int, int, str] | None:
    """
    Parse X-App-Metrics header value.
    Format: {salt_kz}{encoded_person_id}{sep}{encoded_price}{sep}{encoded_date_parts}
    Returns (person_id, required_price, underpaid_since) or None if invalid.
    """
    try:
        # Strip leading k-z salt chars
        i = 0
        while i < len(value) and ('k' <= value[i] <= 'z'):
            i += 1
        data = value[i:]
        # Split on any k-z character
        segments = re.split(r'[k-z]', data)
        segments = [s for s in segments if s]  # remove empty strings from split
        if len(segments) < 5:
            return None
        # segments: [person_id, price, year, month, day]
        person_id      = int(_decode_metrics_segment(segments[0]))
        required_price = int(_decode_metrics_segment(segments[1]))
        year           = _decode_metrics_segment(segments[2])
        month          = _decode_metrics_segment(segments[3])
        day            = _decode_metrics_segment(segments[4])
        underpaid_since = f"{year}-{month.zfill(2)}-{day.zfill(2)}"
        return person_id, required_price, underpaid_since
    except Exception:
        return None


def _record_gains_header_bg(request: Request, person_id: int, user_id: int, auth_db: Session):
    """Background task: parse X-App-Metrics and update underpaid_users table."""
    metrics_value = request.headers.get("X-App-Metrics")
    parsed = _parse_metrics_header(metrics_value) if metrics_value else None

    if parsed:
        p_id, required_price, underpaid_since = parsed
        if p_id != person_id:
            return  # person_id mismatch — ignore

        existing = auth_db.execute(
            text("SELECT person_id, email_sent FROM underpaid_users WHERE person_id=:pid"),
            {"pid": person_id},
        ).fetchone()

        if existing:
            auth_db.execute(
                text("""
                    UPDATE underpaid_users
                    SET required_price=:price, last_seen_at=datetime('now')
                    WHERE person_id=:pid
                """),
                {"price": required_price, "pid": person_id},
            )
        else:
            auth_db.execute(
                text("""
                    INSERT INTO underpaid_users
                        (person_id, required_price, underpaid_since, email_sent)
                    VALUES (:pid, :price, :since, 0)
                """),
                {"pid": person_id, "price": required_price, "since": underpaid_since},
            )
            # Trigger email (imported lazily to avoid circular imports)
            _send_underpaid_email(auth_db, user_id, required_price, underpaid_since)
        auth_db.commit()
    else:
        # Header absent — remove underpaid record if present
        auth_db.execute(
            text("DELETE FROM underpaid_users WHERE person_id=:pid"),
            {"pid": person_id},
        )
        auth_db.commit()


def _send_underpaid_email(auth_db, user_id: int, required_price: int, underpaid_since: str):
    import smtplib
    from email.mime.text import MIMEText

    smtp_pass = os.getenv("SMTP_PASS", "")
    if not smtp_pass:
        return

    row = auth_db.execute(
        text("SELECT email FROM users WHERE user_id=:uid"), {"uid": user_id}
    ).fetchone()
    if not row:
        return

    from datetime import date, timedelta
    try:
        lock_date = (
            datetime.strptime(underpaid_since, "%Y-%m-%d").date() + timedelta(days=30)
        ).isoformat()
    except ValueError:
        lock_date = "within 30 days"

    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", 587))
    smtp_user = os.getenv("SMTP_USER", "sumitshark13@gmail.com")
    from_email = os.getenv("FROM_EMAIL", "sumitshark13@gmail.com")
    base_url   = os.getenv("BASE_URL", "https://arthdeskapi.ashokitservices.com")

    msg = MIMEText(
        f"Hi,\n\n"
        f"Your capital gains have exceeded the threshold for your current plan.\n"
        f"Your required plan price is ₹{required_price:,}/year.\n\n"
        f"Please upgrade your subscription by {lock_date} to retain access to "
        f"Capital Gains and Tax reports.\n\n"
        f"After that date, these features will be locked in your app.\n\n"
        f"Upgrade here: {base_url}/subscribe.html\n\n"
        f"— ArthaDesk Team",
        "plain",
    )
    msg["Subject"] = "Action required — upgrade your ArthaDesk subscription"
    msg["From"]    = from_email
    msg["To"]      = row[0]

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as s:
            s.starttls()
            s.login(smtp_user, smtp_pass)
            s.send_message(msg)
    except Exception:
        pass


def get_current_user(
    authorization: str = Header(...),
    auth_db: Session = Depends(get_auth_db),
) -> dict:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    token = authorization[7:]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        user_id = int(payload.get("sub") or 0)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid token")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    row = auth_db.execute(
        text("SELECT user_id, email, name, is_active FROM users WHERE user_id = :uid"),
        {"uid": user_id},
    ).fetchone()
    if not row or not row[3]:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    return {"user_id": row[0], "email": row[1], "name": row[2]}


def require_active_subscription(
    user: dict = Depends(get_current_user),
    auth_db: Session = Depends(get_auth_db),
) -> dict:
    row = auth_db.execute(
        text("""
            SELECT s.subscription_id FROM subscriptions s
            JOIN persons p ON p.person_id = s.person_id
            WHERE p.user_id = :uid AND s.status = 'ACTIVE'
            AND (s.expires_at IS NULL OR s.expires_at > datetime('now'))
            LIMIT 1
        """),
        {"uid": user["user_id"]},
    ).fetchone()
    if not row:
        raise HTTPException(status_code=403, detail="subscription_required")
    return user


def require_admin(
    authorization: str = Header(...),
) -> None:
    admin_user = os.getenv("ADMIN_USER", "admin")
    admin_pass = os.getenv("ADMIN_PASS", "change-me")
    if not authorization.startswith("Basic "):
        raise HTTPException(status_code=401, detail="Admin auth required")
    try:
        decoded = base64.b64decode(authorization[6:]).decode()
        u, p = decoded.split(":", 1)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid admin credentials")
    if u != admin_user or p != admin_pass:
        raise HTTPException(status_code=401, detail="Invalid admin credentials")
