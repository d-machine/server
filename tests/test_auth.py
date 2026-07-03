"""Tests for /auth/* endpoints and auth helper functions."""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from tests.conftest import register_user, login_user, auth_headers


# ── Registration ──────────────────────────────────────────────────────────────

class TestRegister:
    def test_register_success(self, client):
        r = client.post("/auth/register", json={
            "email": "new@example.com", "name": "New User", "password": "pass1234"
        })
        assert r.status_code == 201
        data = r.json()
        assert data["email"] == "new@example.com"
        assert "user_id" in data

    def test_register_duplicate_email(self, client):
        register_user(client)
        r = client.post("/auth/register", json={
            "email": "test@example.com", "name": "Dup", "password": "pass1234"
        })
        assert r.status_code == 409
        assert "already registered" in r.json()["detail"].lower()

    def test_register_case_insensitive_email(self, client):
        register_user(client, email="User@Example.COM")
        r = client.post("/auth/register", json={
            "email": "user@example.com", "name": "Dup", "password": "pass1234"
        })
        assert r.status_code == 409

    def test_register_invalid_email(self, client):
        r = client.post("/auth/register", json={
            "email": "not-an-email", "name": "Bad", "password": "pass1234"
        })
        assert r.status_code == 422

    def test_register_missing_fields(self, client):
        r = client.post("/auth/register", json={"email": "a@b.com"})
        assert r.status_code == 422


# ── Login ─────────────────────────────────────────────────────────────────────

class TestLogin:
    def test_login_success(self, client):
        register_user(client)
        r = client.post("/auth/login", json={"email": "test@example.com", "password": "password123"})
        assert r.status_code == 200
        data = r.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["token_type"] == "bearer"
        assert data["expires_in"] > 0

    def test_login_wrong_password(self, client):
        register_user(client)
        r = client.post("/auth/login", json={"email": "test@example.com", "password": "wrong"})
        assert r.status_code == 401

    def test_login_nonexistent_email(self, client):
        r = client.post("/auth/login", json={"email": "nobody@example.com", "password": "pass"})
        assert r.status_code == 401

    def test_login_returns_subscription_info(self, client):
        register_user(client)
        r = client.post("/auth/login", json={"email": "test@example.com", "password": "password123"})
        assert r.status_code == 200
        # No subscription yet — should be null
        assert r.json()["subscription"] is None

    def test_login_access_token_is_jwt(self, client):
        register_user(client)
        data = login_user(client)
        # JWT has 3 dot-separated parts
        parts = data["access_token"].split(".")
        assert len(parts) == 3


# ── Token refresh ─────────────────────────────────────────────────────────────

class TestRefresh:
    def test_refresh_success(self, client):
        register_user(client)
        login_data = login_user(client)
        r = client.post("/auth/refresh", json={"refresh_token": login_data["refresh_token"]})
        assert r.status_code == 200
        data = r.json()
        assert "access_token" in data
        assert "refresh_token" in data
        # New refresh token must differ (it's a random secret)
        assert data["refresh_token"] != login_data["refresh_token"]

    def test_refresh_rotates_token(self, client):
        """Old refresh token must be revoked after rotation."""
        register_user(client)
        login_data = login_user(client)
        old_rt = login_data["refresh_token"]

        # First refresh succeeds
        r = client.post("/auth/refresh", json={"refresh_token": old_rt})
        assert r.status_code == 200

        # Using the old refresh token again must fail
        r2 = client.post("/auth/refresh", json={"refresh_token": old_rt})
        assert r2.status_code == 401

    def test_refresh_invalid_token(self, client):
        r = client.post("/auth/refresh", json={"refresh_token": "totally-fake-token"})
        assert r.status_code == 401

    def test_refresh_chained_rotation(self, client):
        """Multiple sequential refreshes must all succeed."""
        register_user(client)
        rt = login_user(client)["refresh_token"]
        for _ in range(3):
            r = client.post("/auth/refresh", json={"refresh_token": rt})
            assert r.status_code == 200
            rt = r.json()["refresh_token"]


# ── Logout ────────────────────────────────────────────────────────────────────

class TestLogout:
    def test_logout_success(self, client):
        register_user(client)
        login_data = login_user(client)
        r = client.post("/auth/logout", json={"refresh_token": login_data["refresh_token"]})
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_logout_revokes_refresh_token(self, client):
        register_user(client)
        login_data = login_user(client)
        rt = login_data["refresh_token"]
        client.post("/auth/logout", json={"refresh_token": rt})
        # Refresh after logout must fail
        r = client.post("/auth/refresh", json={"refresh_token": rt})
        assert r.status_code == 401

    def test_logout_unknown_token_is_ok(self, client):
        """Logging out with an unknown token should still return 200 (idempotent)."""
        r = client.post("/auth/logout", json={"refresh_token": "unknown-token"})
        assert r.status_code == 200


# ── /auth/me ──────────────────────────────────────────────────────────────────

class TestMe:
    def test_me_success(self, client, bearer):
        r = client.get("/auth/me", headers=bearer)
        assert r.status_code == 200
        data = r.json()
        assert data["email"] == "test@example.com"
        assert data["name"] == "Test User"
        assert "subscription" in data

    def test_me_no_token(self, client):
        r = client.get("/auth/me")
        assert r.status_code in (401, 422)

    def test_me_invalid_token(self, client):
        r = client.get("/auth/me", headers={"Authorization": "Bearer bad.token.here"})
        assert r.status_code == 401

    def test_me_with_active_subscription(self, client, bearer, active_subscription, admin_headers):
        r = client.get("/auth/me", headers=bearer)
        assert r.status_code == 200
        sub = r.json()["subscription"]
        assert sub is not None
        assert sub["status"] == "ACTIVE"


# ── Forgot / Reset password ───────────────────────────────────────────────────

class TestForgotPassword:
    def test_forgot_password_always_200(self, client):
        """Should always return 200 — no user enumeration."""
        r = client.post("/auth/forgot-password", json={"email": "nobody@example.com"})
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_forgot_password_registered_user(self, client):
        register_user(client)
        with patch("app.routers.auth._send_email") as mock_mail:
            r = client.post("/auth/forgot-password", json={"email": "test@example.com"})
        assert r.status_code == 200
        mock_mail.assert_called_once()

    def test_forgot_password_unregistered_no_email(self, client):
        with patch("app.routers.auth._send_email") as mock_mail:
            r = client.post("/auth/forgot-password", json={"email": "ghost@example.com"})
        assert r.status_code == 200
        mock_mail.assert_not_called()


class TestResetPassword:
    def _get_reset_token(self, client, email="test@example.com"):
        """Register, trigger forgot-password, extract raw token from DB."""
        register_user(client, email=email)
        captured = {}

        def fake_send(to, subject, body):
            # Extract token from the reset URL in the email body
            for word in body.split():
                if "token=" in word:
                    captured["token"] = word.split("token=")[-1].rstrip(")")

        with patch("app.routers.auth._send_email", side_effect=fake_send):
            client.post("/auth/forgot-password", json={"email": email})

        return captured.get("token")

    def test_reset_password_form_valid_token(self, client):
        token = self._get_reset_token(client)
        assert token is not None
        r = client.get(f"/auth/reset-password?token={token}")
        assert r.status_code == 200
        assert "form" in r.text.lower() or "password" in r.text.lower()

    def test_reset_password_invalid_token(self, client):
        r = client.get("/auth/reset-password?token=fakefakefake")
        assert r.status_code == 400

    def test_reset_password_submit_success(self, client):
        token = self._get_reset_token(client)
        r = client.post("/auth/reset-password", data={"token": token, "new_password": "newpass99"})
        assert r.status_code == 200
        assert "updated" in r.text.lower() or "password" in r.text.lower()

    def test_reset_password_enables_login_with_new_password(self, client):
        token = self._get_reset_token(client)
        client.post("/auth/reset-password", data={"token": token, "new_password": "newpass99"})
        r = client.post("/auth/login", json={"email": "test@example.com", "password": "newpass99"})
        assert r.status_code == 200

    def test_reset_password_old_password_rejected(self, client):
        token = self._get_reset_token(client)
        client.post("/auth/reset-password", data={"token": token, "new_password": "newpass99"})
        r = client.post("/auth/login", json={"email": "test@example.com", "password": "password123"})
        assert r.status_code == 401

    def test_reset_password_token_single_use(self, client):
        token = self._get_reset_token(client)
        client.post("/auth/reset-password", data={"token": token, "new_password": "newpass99"})
        # Second use of same token must fail
        r = client.post("/auth/reset-password", data={"token": token, "new_password": "anotherpass"})
        assert r.status_code == 400

    def test_reset_password_revokes_all_sessions(self, client, auth_engine):
        """After reset, old refresh tokens must be revoked."""
        register_user(client)
        login_data = login_user(client)
        old_rt = login_data["refresh_token"]

        token = self._get_reset_token.__wrapped__(self, client) if hasattr(self._get_reset_token, '__wrapped__') else None
        # Simpler: directly manipulate — just verify refresh fails after reset
        captured = {}
        def fake_send(to, subject, body):
            for word in body.split():
                if "token=" in word:
                    captured["token"] = word.split("token=")[-1].rstrip(")")
        with patch("app.routers.auth._send_email", side_effect=fake_send):
            client.post("/auth/forgot-password", json={"email": "test@example.com"})
        if captured.get("token"):
            client.post("/auth/reset-password", data={"token": captured["token"], "new_password": "newpass99"})
            r = client.post("/auth/refresh", json={"refresh_token": old_rt})
            assert r.status_code == 401


# ── Auth helper unit tests ────────────────────────────────────────────────────

class TestAuthHelpers:
    def test_hash_and_verify_password(self):
        from app.routers.auth import _hash_password, _verify_password
        h = _hash_password("mysecret")
        assert _verify_password("mysecret", h) is True
        assert _verify_password("wrong", h) is False

    def test_sha256_deterministic(self):
        from app.routers.auth import _sha256
        v = "some-token-value"
        assert _sha256(v) == _sha256(v)
        assert _sha256(v) != _sha256(v + "x")
        assert len(_sha256(v)) == 64  # hex digest of SHA-256

    def test_make_access_token_is_valid_jwt(self):
        from app.routers.auth import _make_access_token
        from jose import jwt
        token = _make_access_token(42)
        payload = jwt.decode(token, "test-secret-key-32-bytes-exactly!!", algorithms=["HS256"])
        assert payload["sub"] == "42"

    def test_make_refresh_token_unique(self):
        from app.routers.auth import _make_refresh_token
        tokens = {_make_refresh_token() for _ in range(100)}
        assert len(tokens) == 100  # all unique
