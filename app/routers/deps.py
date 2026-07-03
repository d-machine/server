import os
from fastapi import Depends, Header, HTTPException
from jose import JWTError, jwt
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.auth_db import get_auth_db

JWT_SECRET = os.getenv("JWT_SECRET", "change-me-in-production-use-32-byte-hex")
JWT_ALGORITHM = "HS256"


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
            SELECT subscription_id FROM subscriptions
            WHERE user_id = :uid AND status = 'ACTIVE' AND expires_at > datetime('now')
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
    import base64
    if not authorization.startswith("Basic "):
        raise HTTPException(status_code=401, detail="Admin auth required")
    try:
        decoded = base64.b64decode(authorization[6:]).decode()
        u, p = decoded.split(":", 1)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid admin credentials")
    if u != admin_user or p != admin_pass:
        raise HTTPException(status_code=401, detail="Invalid admin credentials")
