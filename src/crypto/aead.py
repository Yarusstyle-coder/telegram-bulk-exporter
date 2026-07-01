"""AES-256-GCM helpers.

`cryptography`'s AESGCM returns ciphertext with the tag appended; we split the
trailing 16 bytes so the storage format records `nonce`, `ciphertext` (no tag)
and `tag` separately for clarity.
"""

from __future__ import annotations

import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_LEN = 12
TAG_LEN = 16


def encrypt(
    key: bytes,
    plaintext: bytes,
    associated: bytes | None = None,
) -> dict[str, bytes]:
    """Encrypt `plaintext` with AES-256-GCM.

    Args:
        key: 32-byte key.
        plaintext: Raw bytes to encrypt (may be empty).
        associated: Optional AAD (authenticated but not encrypted).

    Returns a dict: ``{"nonce": 12 bytes, "ciphertext": N bytes, "tag": 16 bytes}``.
    """
    if len(key) != 32:
        raise ValueError("key must be 32 bytes (AES-256)")

    nonce = secrets.token_bytes(NONCE_LEN)
    aead = AESGCM(key)
    ct_with_tag = aead.encrypt(nonce, plaintext, associated)
    ciphertext = ct_with_tag[:-TAG_LEN]
    tag = ct_with_tag[-TAG_LEN:]
    return {"nonce": nonce, "ciphertext": ciphertext, "tag": tag}


def decrypt(
    key: bytes,
    nonce: bytes,
    ciphertext: bytes,
    tag: bytes,
    associated: bytes | None = None,
) -> bytes:
    """Decrypt an AES-256-GCM envelope.

    Raises `cryptography.exceptions.InvalidTag` on any tamper / wrong key / wrong AAD.
    """
    if len(key) != 32:
        raise ValueError("key must be 32 bytes (AES-256)")
    if len(nonce) != NONCE_LEN:
        raise ValueError(f"nonce must be {NONCE_LEN} bytes")
    if len(tag) != TAG_LEN:
        raise ValueError(f"tag must be {TAG_LEN} bytes")

    aead = AESGCM(key)
    return aead.decrypt(nonce, ciphertext + tag, associated)
