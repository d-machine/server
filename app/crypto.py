"""
Hybrid RSA-OAEP + AES-256-GCM encryption for sending PII to the desktop app.

Flow:
  1. Server generates a random 256-bit AES key.
  2. Server encrypts the AES key with the desktop's RSA public key (RSA-OAEP / SHA-256).
  3. Server encrypts the plaintext payload with AES-256-GCM (random 96-bit nonce).
  4. Response payload = base64(encrypted_aes_key) + "." + base64(nonce) + "." + base64(ciphertext+tag)

Desktop decrypts in reverse.
"""

import base64
import os

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from cryptography.hazmat.backends import default_backend


def _load_public_key(pem_b64: str) -> RSAPublicKey:
    pem_bytes = base64.b64decode(pem_b64)
    return serialization.load_pem_public_key(pem_bytes, backend=default_backend())


def encrypt_for_desktop(plaintext: str, public_key_b64: str) -> str:
    """
    Encrypt plaintext JSON string with the desktop's RSA public key.
    Returns a dot-separated base64 string: encrypted_aes_key.nonce.ciphertext
    Raises ValueError if the public key is invalid.
    """
    try:
        pub_key = _load_public_key(public_key_b64)
    except Exception as exc:
        raise ValueError(f"Invalid public key: {exc}") from exc

    aes_key = os.urandom(32)
    encrypted_aes_key = pub_key.encrypt(
        aes_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )

    nonce = os.urandom(12)
    aesgcm = AESGCM(aes_key)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode(), None)

    return (
        base64.b64encode(encrypted_aes_key).decode()
        + "."
        + base64.b64encode(nonce).decode()
        + "."
        + base64.b64encode(ciphertext).decode()
    )
