"""Tests for GET/POST/DELETE /persons and GET /persons/secure."""

import base64
import hashlib
import json
from io import BytesIO
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


# ── Helpers ───────────────────────────────────────────────────────────────────

def _generate_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_key, base64.b64encode(pub_pem).decode()


def _decrypt_response(encrypted: str, private_key) -> list:
    enc_aes_key_b64, nonce_b64, ct_b64 = encrypted.split(".")
    enc_aes_key = base64.b64decode(enc_aes_key_b64)
    nonce       = base64.b64decode(nonce_b64)
    ciphertext  = base64.b64decode(ct_b64)

    aes_key = private_key.decrypt(
        enc_aes_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    plaintext = AESGCM(aes_key).decrypt(nonce, ciphertext, None)
    return json.loads(plaintext)


# ── Tests: POST /persons ──────────────────────────────────────────────────────
# The server now receives the raw PAN and computes pan_hash/masked_pan
# itself (salt never leaves the server, see app/routers/persons.py) --
# callers send {pan, display_name}, never pan_hash/masked_pan directly.

def test_create_person(client, bearer):
    r = client.post(
        "/persons",
        json={"pan": "ABCDE1234F", "display_name": "Alice"},
        headers=bearer,
    )
    assert r.status_code == 201
    data = r.json()
    assert data["display_name"] == "Alice"
    assert "person_id" in data


def test_create_person_duplicate_rejected(client, bearer):
    payload = {"pan": "ABCDE1234F", "display_name": "Alice"}
    client.post("/persons", json=payload, headers=bearer)
    r = client.post("/persons", json=payload, headers=bearer)
    assert r.status_code == 409


def test_create_person_rejects_invalid_pan_format(client, bearer):
    r = client.post(
        "/persons",
        json={"pan": "NOT-A-REAL-PAN", "display_name": "Alice"},
        headers=bearer,
    )
    assert r.status_code == 422


def test_create_person_normalises_case_for_duplicate_check(client, bearer):
    """A lowercase/whitespace-padded PAN must still be recognised as the
    same PAN as its canonical uppercase form (same pan_hash either way)."""
    client.post("/persons", json={"pan": "ABCDE1234F", "display_name": "Alice"}, headers=bearer)
    r = client.post("/persons", json={"pan": " abcde1234f ", "display_name": "Alice 2"}, headers=bearer)
    assert r.status_code == 409


def test_create_person_hash_uses_per_user_salt(client, auth_engine):
    """Two different users registering the SAME real PAN must get different
    pan_hash values -- proof the salt is actually per-user, not a constant,
    and that it's applied server-side (callers never see or supply it)."""
    from tests.conftest import register_user, login_user

    register_user(client, email="userA@example.com")
    bearer_a = {"Authorization": f"Bearer {login_user(client, email='userA@example.com')['access_token']}"}
    register_user(client, email="userB@example.com")
    bearer_b = {"Authorization": f"Bearer {login_user(client, email='userB@example.com')['access_token']}"}

    client.post("/persons", json={"pan": "ABCDE1234F", "display_name": "A"}, headers=bearer_a)
    client.post("/persons", json={"pan": "ABCDE1234F", "display_name": "B"}, headers=bearer_b)

    with auth_engine.connect() as conn:
        from sqlalchemy import text
        rows = conn.execute(text("SELECT display_name, pan_hash FROM persons ORDER BY display_name")).fetchall()
    assert len(rows) == 2
    assert rows[0][1] != rows[1][1]  # same PAN, different hash per user


def test_create_person_requires_auth(client):
    r = client.post(
        "/persons",
        json={"pan": "ABCDE1234F", "display_name": "X"},
    )
    # Was 422 (Header(...) failed FastAPI's own request validation before
    # get_current_user ran) -- fixed in deps.py to return the intended 401.
    assert r.status_code == 401


# ── Tests: GET /persons (website, no masked_pan) ─────────────────────────────

def test_list_persons_no_masked_pan(client, bearer, person_id):
    r = client.get("/persons", headers=bearer)
    assert r.status_code == 200
    persons = r.json()
    assert len(persons) == 1
    assert "masked_pan" not in persons[0]
    assert "pan_hash" not in persons[0]
    assert persons[0]["display_name"] == "Test Person"


def test_list_persons_empty(client, bearer):
    r = client.get("/persons", headers=bearer)
    assert r.status_code == 200
    assert r.json() == []


# ── Tests: GET /persons/secure (desktop, encrypted) ──────────────────────────

def test_list_persons_secure_returns_encrypted(client, bearer, person_id):
    private_key, pub_key_b64 = _generate_keypair()
    r = client.get("/persons/secure", headers={**bearer, "X-Public-Key": pub_key_b64})
    assert r.status_code == 200
    encrypted = r.json()["data"]
    assert "." in encrypted  # dot-separated format

    persons = _decrypt_response(encrypted, private_key)
    assert len(persons) == 1
    # person_id fixture creates with pan="ABCDE1234F" -- masked_pan is
    # deterministic from that; pan_hash is salted server-side so only its
    # shape (sha512 hex digest), not an exact value, is checked here.
    assert persons[0]["masked_pan"] == "ABCDE****F"
    assert len(persons[0]["pan_hash"]) == 128
    assert persons[0]["display_name"] == "Test Person"


def test_list_persons_secure_invalid_key(client, bearer):
    r = client.get("/persons/secure", headers={**bearer, "X-Public-Key": "bm90YXZhbGlka2V5"})
    assert r.status_code == 400


def test_list_persons_secure_missing_key_header(client, bearer):
    r = client.get("/persons/secure", headers=bearer)
    assert r.status_code == 422


# ── Tests: DELETE /persons/{id} ───────────────────────────────────────────────

def test_delete_person(client, bearer, person_id):
    r = client.delete(f"/persons/{person_id}", headers=bearer)
    assert r.status_code == 204
    # Confirm gone
    r = client.get("/persons", headers=bearer)
    assert r.json() == []


def test_delete_person_not_found(client, bearer):
    r = client.delete("/persons/9999", headers=bearer)
    assert r.status_code == 404


def test_delete_person_with_active_subscription_rejected(client, bearer, active_subscription, person_id):
    r = client.delete(f"/persons/{person_id}", headers=bearer)
    assert r.status_code == 409


def test_cannot_delete_other_users_person(client, bearer):
    # Create second user
    from tests.conftest import register_user, login_user, create_person
    register_user(client, email="other@example.com")
    other_login = login_user(client, email="other@example.com")
    other_bearer = {"Authorization": f"Bearer {other_login['access_token']}"}
    other_pid = create_person(client, other_bearer, pan="FGHIJ5678K")

    # First user tries to delete it
    r = client.delete(f"/persons/{other_pid}", headers=bearer)
    assert r.status_code == 404
