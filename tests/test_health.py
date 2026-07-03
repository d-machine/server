"""Tests for health check and root endpoints."""

import base64
import pytest


class TestHealth:
    def test_health_endpoint(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data.get("status") in ("ok", "healthy", "up")

    def test_root_redirect_or_ok(self, client):
        """Root may serve the website index or a redirect — just shouldn't 500."""
        r = client.get("/", follow_redirects=False)
        assert r.status_code < 500


class TestAdminEndpoints:
    def test_admin_list_subscriptions_valid_creds(self, client, admin_headers):
        r = client.get("/subscriptions/admin", headers=admin_headers)
        assert r.status_code == 200

    def test_admin_list_subscriptions_invalid_creds(self, client):
        creds = base64.b64encode(b"admin:wrong").decode()
        r = client.get("/subscriptions/admin", headers={"Authorization": f"Basic {creds}"})
        assert r.status_code == 401

    def test_admin_list_subscriptions_no_creds(self, client):
        r = client.get("/subscriptions/admin")
        assert r.status_code in (401, 422)

    def test_gated_endpoint_without_any_auth(self, client):
        r = client.get("/prices/latest")
        assert r.status_code in (401, 422)

    def test_gated_endpoint_with_user_token_no_sub(self, client, bearer):
        r = client.get("/prices/latest", headers=bearer)
        assert r.status_code == 403
