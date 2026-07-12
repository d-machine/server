import hashlib
import os
import secrets
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from jose import jwt
from pydantic import BaseModel, EmailStr
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth_db import get_auth_db
from app.routers.deps import JWT_ALGORITHM, JWT_SECRET, get_current_user, require_admin

router = APIRouter()

JWT_ACCESS_EXPIRES_SECONDS = int(os.getenv("JWT_ACCESS_EXPIRES_SECONDS", 86400))   # 1 day
JWT_REFRESH_EXPIRES_DAYS   = int(os.getenv("JWT_REFRESH_EXPIRES_DAYS", 30))

SMTP_HOST  = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT  = int(os.getenv("SMTP_PORT", 587))
SMTP_USER  = os.getenv("SMTP_USER", "sumitshark13@gmail.com")
SMTP_PASS  = os.getenv("SMTP_PASS", "")
FROM_EMAIL = os.getenv("FROM_EMAIL", "sumitshark13@gmail.com")
BASE_URL   = os.getenv("BASE_URL", "https://arthdeskapi.ashokitservices.com")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


def _make_access_token(user_id: int) -> str:
    exp = datetime.now(timezone.utc) + timedelta(seconds=JWT_ACCESS_EXPIRES_SECONDS)
    return jwt.encode({"sub": str(user_id), "exp": exp}, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _make_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _store_refresh_token(auth_db: Session, user_id: int, raw_token: str) -> int:
    expires_at = (datetime.now(timezone.utc) + timedelta(days=JWT_REFRESH_EXPIRES_DAYS)).isoformat()
    result = auth_db.execute(
        text("""
            INSERT INTO refresh_tokens (user_id, token_hash, expires_at)
            VALUES (:uid, :hash, :exp)
        """),
        {"uid": user_id, "hash": _sha256(raw_token), "exp": expires_at},
    )
    auth_db.commit()
    return result.lastrowid


def _subscription_info(auth_db: Session, user_id: int) -> dict | None:
    row = auth_db.execute(
        text("""
            SELECT plan, status, expires_at
            FROM subscriptions
            WHERE user_id = :uid
            ORDER BY created_at DESC LIMIT 1
        """),
        {"uid": user_id},
    ).fetchone()
    if not row:
        return None
    return {"plan": row[0], "status": row[1], "expires_at": row[2]}


def _send_email(to: str, subject: str, body: str):
    if not SMTP_PASS:
        return  # skip silently in dev if not configured
    msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"]    = FROM_EMAIL
    msg["To"]      = to
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    email: EmailStr
    name: str
    password: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/admin/check")
def admin_check(_: None = Depends(require_admin)):
    """Lightweight endpoint for admin panel login verification."""
    return {"ok": True}


@router.post("/register", status_code=201)
def register(req: RegisterRequest, auth_db: Session = Depends(get_auth_db)):
    existing = auth_db.execute(
        text("SELECT user_id FROM users WHERE email = :email"),
        {"email": req.email},
    ).fetchone()
    if existing:
        raise HTTPException(status_code=409, detail="Email already registered")

    pan_salt = secrets.token_hex(16)
    auth_db.execute(
        text("INSERT INTO users (email, name, password_hash, pan_salt) VALUES (:email, :name, :hash, :salt)"),
        {"email": req.email, "name": req.name, "hash": _hash_password(req.password), "salt": pan_salt},
    )
    auth_db.commit()
    row = auth_db.execute(
        text("SELECT user_id FROM users WHERE email = :email"),
        {"email": req.email},
    ).fetchone()
    return {"user_id": row[0], "email": req.email}


@router.post("/login")
def login(req: LoginRequest, auth_db: Session = Depends(get_auth_db)):
    row = auth_db.execute(
        text("SELECT user_id, password_hash, is_active FROM users WHERE email = :email"),
        {"email": req.email},
    ).fetchone()
    if not row or not _verify_password(req.password, row[1]) or not row[2]:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    user_id = row[0]
    access_token  = _make_access_token(user_id)
    refresh_token = _make_refresh_token()
    _store_refresh_token(auth_db, user_id, refresh_token)

    return {
        "access_token":  access_token,
        "refresh_token": refresh_token,
        "token_type":    "bearer",
        "expires_in":    JWT_ACCESS_EXPIRES_SECONDS,
        "subscription":  _subscription_info(auth_db, user_id),
    }


@router.post("/refresh")
def refresh(req: RefreshRequest, auth_db: Session = Depends(get_auth_db)):
    token_hash = _sha256(req.refresh_token)
    row = auth_db.execute(
        text("""
            SELECT token_id, user_id, revoked, expires_at
            FROM refresh_tokens WHERE token_hash = :hash
        """),
        {"hash": token_hash},
    ).fetchone()

    if not row or row[2]:  # not found or revoked
        raise HTTPException(status_code=401, detail="Invalid or revoked refresh token")

    expires_at = datetime.fromisoformat(row[3])
    if expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail="Refresh token expired")

    old_token_id = row[0]
    user_id      = row[1]

    # Issue new tokens
    new_access  = _make_access_token(user_id)
    new_refresh = _make_refresh_token()
    new_id      = _store_refresh_token(auth_db, user_id, new_refresh)

    # Revoke old token and link to new one
    auth_db.execute(
        text("UPDATE refresh_tokens SET revoked=1, replaced_by=:new_id WHERE token_id=:old_id"),
        {"new_id": new_id, "old_id": old_token_id},
    )
    auth_db.commit()

    return {
        "access_token":  new_access,
        "refresh_token": new_refresh,
        "token_type":    "bearer",
        "expires_in":    JWT_ACCESS_EXPIRES_SECONDS,
    }


@router.post("/logout")
def logout(req: LogoutRequest, auth_db: Session = Depends(get_auth_db)):
    token_hash = _sha256(req.refresh_token)
    auth_db.execute(
        text("UPDATE refresh_tokens SET revoked=1 WHERE token_hash=:hash"),
        {"hash": token_hash},
    )
    auth_db.commit()
    return {"ok": True}


@router.get("/me")
def me(user: dict = Depends(get_current_user), auth_db: Session = Depends(get_auth_db)):
    row = auth_db.execute(
        text("SELECT pan_salt FROM users WHERE user_id = :uid"),
        {"uid": user["user_id"]},
    ).fetchone()
    pan_salt = row[0] if row else ""
    return {**user, "pan_salt": pan_salt, "subscription": _subscription_info(auth_db, user["user_id"])}


@router.post("/forgot-password")
def forgot_password(req: ForgotPasswordRequest, auth_db: Session = Depends(get_auth_db)):
    row = auth_db.execute(
        text("SELECT user_id FROM users WHERE email = :email AND is_active=1"),
        {"email": req.email},
    ).fetchone()
    if row:
        raw_token  = secrets.token_urlsafe(32)
        token_hash = _sha256(raw_token)
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        auth_db.execute(
            text("""
                INSERT INTO password_reset_tokens (user_id, token_hash, expires_at)
                VALUES (:uid, :hash, :exp)
            """),
            {"uid": row[0], "hash": token_hash, "exp": expires_at},
        )
        auth_db.commit()
        reset_url = f"{BASE_URL}/auth/reset-password?token={raw_token}"
        _send_email(
            to=req.email,
            subject="Reset your Portfolio Tracker password",
            body=(
                f"Hi,\n\nClick the link below to reset your password. "
                f"This link expires in 30 minutes.\n\n{reset_url}\n\n"
                "If you didn't request this, ignore this email."
            ),
        )
    # Always 200 — no user enumeration
    return {"ok": True}


_RESET_FORM_HTML = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Reset Password</title>
<script src="https://cdn.tailwindcss.com"></script></head>
<body class="min-h-screen bg-gray-50 flex items-center justify-center p-4">
<div class="bg-white rounded-xl shadow p-8 w-full max-w-md">
  <h1 class="text-xl font-semibold mb-6">Set new password</h1>
  {message}
  <form method="POST" action="/auth/reset-password" class="{form_class}">
    <input type="hidden" name="token" value="{token}">
    <label class="block text-sm font-medium mb-1">New Password</label>
    <input type="password" name="new_password" required minlength="8"
      class="w-full border rounded-lg px-3 py-2 mb-4 focus:outline-none focus:ring-2 focus:ring-blue-500">
    <button type="submit"
      class="w-full bg-blue-600 text-white rounded-lg py-2 font-medium hover:bg-blue-700">
      Update Password
    </button>
  </form>
</div>
</body></html>"""


@router.get("/reset-password", response_class=HTMLResponse)
def reset_password_form(token: str, auth_db: Session = Depends(get_auth_db)):
    token_hash = _sha256(token)
    row = auth_db.execute(
        text("SELECT token_id, expires_at, used FROM password_reset_tokens WHERE token_hash=:hash"),
        {"hash": token_hash},
    ).fetchone()
    if not row or row[2] or datetime.fromisoformat(row[1]) < datetime.now(timezone.utc):
        html = _RESET_FORM_HTML.format(
            token=token,
            message='<p class="text-red-600 mb-4">This link is invalid or has expired.</p>',
            form_class="hidden",
        )
        return HTMLResponse(html, status_code=400)
    html = _RESET_FORM_HTML.format(token=token, message="", form_class="")
    return HTMLResponse(html)


@router.post("/reset-password", response_class=HTMLResponse)
async def reset_password(request: Request, auth_db: Session = Depends(get_auth_db)):
    form = await request.form()
    raw_token    = form.get("token", "")
    new_password = form.get("new_password", "")

    token_hash = _sha256(raw_token)
    row = auth_db.execute(
        text("""
            SELECT t.token_id, t.user_id, t.expires_at, t.used
            FROM password_reset_tokens t WHERE t.token_hash=:hash
        """),
        {"hash": token_hash},
    ).fetchone()

    if not row or row[3] or datetime.fromisoformat(row[2]) < datetime.now(timezone.utc):
        return HTMLResponse(
            "<p>Invalid or expired link. Please request a new one.</p>", status_code=400
        )

    token_id = row[0]
    user_id  = row[1]
    new_hash = _hash_password(new_password)

    auth_db.execute(
        text("UPDATE users SET password_hash=:h WHERE user_id=:uid"),
        {"h": new_hash, "uid": user_id},
    )
    auth_db.execute(
        text("UPDATE password_reset_tokens SET used=1 WHERE token_id=:tid"),
        {"tid": token_id},
    )
    # Revoke all refresh tokens for this user (log out all devices)
    auth_db.execute(
        text("UPDATE refresh_tokens SET revoked=1 WHERE user_id=:uid"),
        {"uid": user_id},
    )
    auth_db.commit()

    return HTMLResponse("""<!DOCTYPE html>
<html><head><script src="https://cdn.tailwindcss.com"></script></head>
<body class="min-h-screen bg-gray-50 flex items-center justify-center p-4">
<div class="bg-white rounded-xl shadow p-8 w-full max-w-md text-center">
  <h1 class="text-xl font-semibold mb-4 text-green-600">Password updated!</h1>
  <p class="text-gray-600">You can now log in with your new password in the app or on this website.</p>
  <a href="/auth.html" class="mt-6 inline-block bg-blue-600 text-white rounded-lg px-6 py-2 font-medium hover:bg-blue-700">
    Go to Login
  </a>
</div></body></html>""")
