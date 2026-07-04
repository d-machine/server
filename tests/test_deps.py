"""Tests for dependency injection functions in deps.py."""

import base64
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from tests.conftest import register_user, login_user


class TestGetCurrentUser:
    def test_valid_token_returns_user(self, client, bearer):
        """A valid Bearer JWT should identify the user."""
        r = client.get("/auth/me", headers=bearer)
        assert r.status_code == 200
        assert "user_id" in r.json()

    def test_missing_authorization_header(self, client):
        r = client.get("/auth/me")
        assert r.status_code in (401, 422)

    def test_malformed_bearer(self, client):
        r = client.get("/auth/me", headers={"Authorization": "NotBearer token"})
        assert r.status_code == 401

    def test_invalid_jwt(self, client):
        r = client.get("/auth/me", headers={"Authorization": "Bearer invalid.jwt.token"})
        assert r.status_code == 401

    def test_expired_jwt(self, client, registered_user):
        """A JWT with exp in the past should be rejected."""
        from jose import jwt
        import os
        secret = os.environ["JWT_SECRET"]
        payload = {
            "sub": 1,
            "exp": datetime.now(timezone.utc) - timedelta(seconds=1),
        }
        expired_token = jwt.encode(payload, secret, algorithm="HS256")
        r = client.get("/auth/me", headers={"Authorization": f"Bearer {expired_token}"})
        assert r.status_code == 401

    def test_token_for_deleted_user(self, client, auth_engine):
        """Token referencing a non-existent user_id should be 401."""
        from jose import jwt
        import os
        secret = os.environ["JWT_SECRET"]
        payload = {
            "sub": 9999,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        }
        token = jwt.encode(payload, secret, algorithm="HS256")
        r = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401


class TestRequireActiveSubscription:
    def test_without_subscription_raises_403(self, client, bearer):
        """Gated endpoints should return 403 when no active subscription exists."""
        r = client.get("/prices/latest", headers=bearer)
        assert r.status_code == 403
        assert r.json()["detail"] == "subscription_required"

    def test_with_active_subscription_passes(self, client, bearer_with_sub):
        """User with an ACTIVE subscription should pass the gate."""
        r = client.get("/prices/latest", headers=bearer_with_sub)
        # DB is empty so it might return 200 with empty data — what matters is NOT 403
        assert r.status_code != 403

    def test_pending_subscription_blocked(self, client, bearer, person_id):
        """PENDING_APPROVAL status should NOT grant access."""
        from io import BytesIO
        import json as _json
        persons_payload = _json.dumps([{"person_id": person_id, "amount": 1000}])
        client.post(
            "/subscriptions/submit",
            data={"persons": persons_payload},
            files={"screenshot": ("f.png", BytesIO(b"x"), "image/png")},
            headers=bearer,
        )
        r = client.get("/prices/latest", headers=bearer)
        assert r.status_code == 403

    def test_declined_subscription_blocked(self, client, bearer, admin_headers, person_id):
        from io import BytesIO
        import json as _json
        persons_payload = _json.dumps([{"person_id": person_id, "amount": 1000}])
        resp = client.post(
            "/subscriptions/submit",
            data={"persons": persons_payload},
            files={"screenshot": ("f.png", BytesIO(b"x"), "image/png")},
            headers=bearer,
        )
        sub_id = resp.json()["created"][0]["subscription_id"]
        client.post(f"/subscriptions/admin/{sub_id}/decline",
                    data={"reason": "Bad"}, headers=admin_headers)
        r = client.get("/prices/latest", headers=bearer)
        assert r.status_code == 403


class TestRequireAdmin:
    def test_valid_admin_credentials(self, client, admin_headers):
        r = client.get("/subscriptions/admin", headers=admin_headers)
        assert r.status_code == 200

    def test_wrong_admin_password(self, client):
        creds = base64.b64encode(b"admin:wrongpassword").decode()
        r = client.get("/subscriptions/admin", headers={"Authorization": f"Basic {creds}"})
        assert r.status_code == 401

    def test_wrong_admin_username(self, client):
        creds = base64.b64encode(b"notadmin:adminpass").decode()
        r = client.get("/subscriptions/admin", headers={"Authorization": f"Basic {creds}"})
        assert r.status_code == 401

    def test_no_auth_header(self, client):
        r = client.get("/subscriptions/admin")
        assert r.status_code in (401, 422)

    def test_bearer_token_not_accepted_for_admin(self, client, bearer):
        r = client.get("/subscriptions/admin", headers=bearer)
        assert r.status_code == 401
