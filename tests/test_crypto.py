"""Tests for server-side hybrid RSA-OAEP + AES-256-GCM crypto helpers."""

import base64
import json

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.crypto import encrypt_for_desktop


def _make_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_key, base64.b64encode(pub_pem).decode()


def _decrypt(encrypted: str, private_key) -> str:
    enc_aes_b64, nonce_b64, ct_b64 = encrypted.split(".")
    aes_key = private_key.decrypt(
        base64.b64decode(enc_aes_b64),
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    plaintext = AESGCM(aes_key).decrypt(
        base64.b64decode(nonce_b64),
        base64.b64decode(ct_b64),
        None,
    )
    return plaintext.decode()


def test_encrypt_decrypt_roundtrip():
    private_key, pub_b64 = _make_keypair()
    payload = json.dumps({"hello": "world", "number": 42})
    encrypted = encrypt_for_desktop(payload, pub_b64)
    assert isinstance(encrypted, str)
    assert encrypted.count(".") == 2
    decrypted = _decrypt(encrypted, private_key)
    assert decrypted == payload


def test_each_encryption_is_unique():
    _, pub_b64 = _make_keypair()
    payload = "same payload"
    enc1 = encrypt_for_desktop(payload, pub_b64)
    enc2 = encrypt_for_desktop(payload, pub_b64)
    assert enc1 != enc2  # random nonce + random AES key each time


def test_invalid_public_key_raises():
    with pytest.raises(ValueError, match="Invalid public key"):
        encrypt_for_desktop("data", "bm90YXZhbGlka2V5")


def test_tampered_ciphertext_fails_decryption():
    private_key, pub_b64 = _make_keypair()
    encrypted = encrypt_for_desktop("secret", pub_b64)
    parts = encrypted.split(".")
    # Corrupt the ciphertext part
    ct_bytes = base64.b64decode(parts[2])
    tampered = bytearray(ct_bytes)
    tampered[0] ^= 0xFF
    parts[2] = base64.b64encode(bytes(tampered)).decode()
    corrupted = ".".join(parts)

    with pytest.raises(Exception):
        _decrypt(corrupted, private_key)
