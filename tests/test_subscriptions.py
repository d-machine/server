"""Tests for /subscriptions/* endpoints (new ticket-based subscription model).

The old screenshot/GCS/approval flow has been replaced with a ticket-based model
where payment evidence is submitted as tickets and admin approves/declines tickets.
Person creation auto-creates a 30-day TRIAL subscription.

Endpoints tested:
  GET  /subscriptions/status          — user's subscription state
  GET  /subscriptions/history         — approved payment history
  GET  /subscriptions/admin/users     — admin: list all users
  GET  /subscriptions/admin/users/{id} — admin: get single user
  GET  /subscriptions/admin/persons   — admin: list all persons
  POST /subscriptions/admin/persons/{id}/block   — cancel subscription
  POST /subscriptions/admin/persons/{id}/unblock — activate subscription
"""

import pytest

from tests.conftest import register_user, login_user, create_person


# ── Status endpoint ───────────────────────────────────────────────────────────

class TestSubscriptionStatus:
    def test_status_no_persons(self, client, bearer):
        """User with no persons gets has_subscription=False."""
        r = client.get("/subscriptions/status", headers=bearer)
        assert r.status_code == 200
        assert r.json().get("has_subscription") is False

    def test_status_after_person_creation(self, client, bearer, person_id):
        """Person creation auto-creates TRIAL subscription."""
        r = client.get("/subscriptions/status", headers=bearer)
        assert r.status_code == 200
        data = r.json()
        assert data["has_subscription"] is True
        person = data["persons"][0]
        assert person["person_id"] == person_id
        assert person["status"] == "TRIAL"

    def test_status_after_activation(self, client, bearer, active_subscription):
        """After admin unblock, status becomes ACTIVE."""
        r = client.get("/subscriptions/status", headers=bearer)
        assert r.status_code == 200
        data = r.json()
        assert data["has_subscription"] is True
        assert data["persons"][0]["status"] == "ACTIVE"

    def test_status_after_block(self, client, bearer, admin_headers, active_subscription, person_id):
        """After admin block, status becomes CANCELLED."""
        client.post(f"/subscriptions/admin/persons/{person_id}/block", headers=admin_headers)
        r = client.get("/subscriptions/status", headers=bearer)
        assert r.json()["persons"][0]["status"] == "CANCELLED"

    def test_status_requires_auth(self, client):
        r = client.get("/subscriptions/status")
        assert r.status_code in (401, 422)

    def test_status_has_person_display_name(self, client, bearer, person_id):
        r = client.get("/subscriptions/status", headers=bearer)
        person = r.json()["persons"][0]
        assert "display_name" in person
        assert "expires_at" in person


# ── History endpoint ──────────────────────────────────────────────────────────

class TestPaymentHistory:
    def test_history_empty_for_new_user(self, client, bearer):
        r = client.get("/subscriptions/history", headers=bearer)
        assert r.status_code == 200
        assert r.json() == []

    def test_history_requires_auth(self, client):
        r = client.get("/subscriptions/history")
        assert r.status_code in (401, 422)

    def test_history_structure(self, client, bearer):
        """Even with no tickets, endpoint returns a list."""
        r = client.get("/subscriptions/history", headers=bearer)
        assert isinstance(r.json(), list)


# ── Admin: list users ─────────────────────────────────────────────────────────

class TestAdminListUsers:
    def test_list_returns_users(self, client, admin_headers, bearer):
        r = client.get("/subscriptions/admin/users", headers=admin_headers)
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert len(data) >= 1

    def test_list_requires_admin(self, client, bearer):
        r = client.get("/subscriptions/admin/users", headers=bearer)
        assert r.status_code == 401

    def test_list_user_fields(self, client, admin_headers, bearer):
        r = client.get("/subscriptions/admin/users", headers=admin_headers)
        user = r.json()[0]
        assert "user_id" in user
        assert "email" in user
        assert "name" in user
        assert "person_count" in user
        assert "total_paid" in user

    def test_list_increments_person_count(self, client, admin_headers, bearer, person_id):
        r = client.get("/subscriptions/admin/users", headers=admin_headers)
        user = next(u for u in r.json() if u["email"] == "test@example.com")
        assert user["person_count"] >= 1


# ── Admin: get single user ────────────────────────────────────────────────────

class TestAdminGetUser:
    def test_get_user_detail(self, client, admin_headers, bearer, person_id):
        # Find our user_id from the list
        users = client.get("/subscriptions/admin/users", headers=admin_headers).json()
        uid = next(u["user_id"] for u in users if u["email"] == "test@example.com")

        r = client.get(f"/subscriptions/admin/users/{uid}", headers=admin_headers)
        assert r.status_code == 200
        data = r.json()
        assert data["user_id"] == uid
        assert "persons" in data
        assert len(data["persons"]) >= 1

    def test_get_user_not_found(self, client, admin_headers):
        r = client.get("/subscriptions/admin/users/9999", headers=admin_headers)
        assert r.status_code == 404

    def test_get_user_requires_admin(self, client, bearer):
        r = client.get("/subscriptions/admin/users/1", headers=bearer)
        assert r.status_code == 401


# ── Admin: list persons ───────────────────────────────────────────────────────

class TestAdminListPersons:
    def test_list_persons(self, client, admin_headers, bearer, person_id):
        r = client.get("/subscriptions/admin/persons", headers=admin_headers)
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert any(p["person_id"] == person_id for p in data)

    def test_list_persons_requires_admin(self, client, bearer):
        r = client.get("/subscriptions/admin/persons", headers=bearer)
        assert r.status_code == 401

    def test_list_persons_filter_by_status(self, client, admin_headers, bearer, active_subscription, person_id):
        r = client.get("/subscriptions/admin/persons?status=ACTIVE", headers=admin_headers)
        assert r.status_code == 200
        for person in r.json():
            assert person["status"] == "ACTIVE"

    def test_list_persons_fields(self, client, admin_headers, bearer, person_id):
        r = client.get("/subscriptions/admin/persons", headers=admin_headers)
        person = next(p for p in r.json() if p["person_id"] == person_id)
        assert "display_name" in person
        assert "user_id" in person
        assert "user_email" in person
        assert "status" in person


# ── Admin: block / unblock ────────────────────────────────────────────────────

class TestAdminBlockUnblock:
    def test_block_sets_cancelled(self, client, admin_headers, bearer, active_subscription, person_id):
        r = client.post(f"/subscriptions/admin/persons/{person_id}/block", headers=admin_headers)
        assert r.status_code == 200
        assert r.json()["status"] == "CANCELLED"

    def test_unblock_sets_active(self, client, admin_headers, person_id):
        # person_id has TRIAL subscription from create_person; promote to ACTIVE
        r = client.post(f"/subscriptions/admin/persons/{person_id}/unblock", headers=admin_headers)
        assert r.status_code == 200
        assert r.json()["status"] == "ACTIVE"

    def test_block_then_unblock(self, client, admin_headers, bearer, active_subscription, person_id):
        client.post(f"/subscriptions/admin/persons/{person_id}/block", headers=admin_headers)
        r = client.post(f"/subscriptions/admin/persons/{person_id}/unblock", headers=admin_headers)
        assert r.status_code == 200
        assert r.json()["status"] == "ACTIVE"

    def test_block_nonexistent_person(self, client, admin_headers):
        r = client.post("/subscriptions/admin/persons/9999/block", headers=admin_headers)
        assert r.status_code == 404

    def test_unblock_nonexistent_person(self, client, admin_headers):
        r = client.post("/subscriptions/admin/persons/9999/unblock", headers=admin_headers)
        assert r.status_code == 404

    def test_block_requires_admin(self, client, bearer, person_id):
        r = client.post(f"/subscriptions/admin/persons/{person_id}/block", headers=bearer)
        assert r.status_code == 401

    def test_unblock_requires_admin(self, client, bearer, person_id):
        r = client.post(f"/subscriptions/admin/persons/{person_id}/unblock", headers=bearer)
        assert r.status_code == 401

    def test_block_is_reflected_in_status(self, client, admin_headers, bearer, active_subscription, person_id):
        client.post(f"/subscriptions/admin/persons/{person_id}/block", headers=admin_headers)
        r = client.get("/subscriptions/status", headers=bearer)
        assert r.json()["persons"][0]["status"] == "CANCELLED"

    def test_trial_subscription_visible_in_persons_list(self, client, admin_headers, bearer, person_id):
        """A freshly created person shows up in admin/persons with TRIAL status."""
        r = client.get("/subscriptions/admin/persons", headers=admin_headers)
        person = next((p for p in r.json() if p["person_id"] == person_id), None)
        assert person is not None
        assert person["status"] == "TRIAL"
