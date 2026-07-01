"""Thin helpers to persist Telegram API credentials and phone number.

Each value is serialised as UTF-8 bytes, then sealed with AES-256-GCM using the
session DEK. The envelope is concatenated as ``nonce(12) | ciphertext | tag(16)``
and stored in the corresponding ``BLOB`` column of the ``user_secrets`` table
so the schema stays tidy and matches the ``UserSecret`` docstring.

A singleton row (``id=1``) is maintained. The row is created if absent on the
first save.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from src.crypto.aead import NONCE_LEN, TAG_LEN
from src.crypto.aead import decrypt as aead_decrypt
from src.crypto.aead import encrypt as aead_encrypt
from src.db.models import UserSecret
from src.logging_setup import get_logger

log = get_logger(__name__)


def _seal(dek: bytes, plaintext: bytes) -> bytes:
    """Seal `plaintext` with the DEK and return `nonce | ciphertext | tag`."""
    env = aead_encrypt(dek, plaintext)
    return env["nonce"] + env["ciphertext"] + env["tag"]


def _unseal(dek: bytes, blob: bytes) -> bytes:
    """Reverse of :func:`_seal`. Raises ``InvalidTag`` on wrong DEK."""
    if len(blob) < NONCE_LEN + TAG_LEN:
        raise ValueError("sealed blob too short")
    nonce = blob[:NONCE_LEN]
    tag = blob[-TAG_LEN:]
    ciphertext = blob[NONCE_LEN:-TAG_LEN]
    return aead_decrypt(dek, nonce, ciphertext, tag)


async def _get_or_create_row(session: Any) -> UserSecret:
    row = (
        await session.execute(select(UserSecret).where(UserSecret.id == 1))
    ).scalar_one_or_none()
    if row is None:
        row = UserSecret(id=1)
        session.add(row)
    return row


async def save_api_credentials(
    session_factory: Any,
    dek: bytes,
    api_id: int,
    api_hash: str,
) -> None:
    """Persist api_id + api_hash, AES-GCM-encrypted with `dek`."""
    async with session_factory() as s:
        row = await _get_or_create_row(s)
        row.enc_api_id = _seal(dek, str(int(api_id)).encode("utf-8"))
        row.enc_api_hash = _seal(dek, api_hash.encode("utf-8"))
        await s.commit()
    log.info("telegram_api_credentials_saved")


async def load_api_credentials(
    session_factory: Any,
    dek: bytes,
) -> tuple[int, str] | None:
    """Return decrypted (api_id, api_hash) or ``None`` if not yet stored."""
    async with session_factory() as s:
        row = (
            await s.execute(select(UserSecret).where(UserSecret.id == 1))
        ).scalar_one_or_none()
        if row is None or row.enc_api_id is None or row.enc_api_hash is None:
            return None
        api_id_bytes = _unseal(dek, row.enc_api_id)
        api_hash_bytes = _unseal(dek, row.enc_api_hash)
    try:
        api_id = int(api_id_bytes.decode("utf-8"))
    except ValueError as exc:  # pragma: no cover - corruption path
        raise ValueError("stored api_id is not an integer") from exc
    return api_id, api_hash_bytes.decode("utf-8")


async def save_phone(session_factory: Any, dek: bytes, phone: str) -> None:
    """Persist the E.164 phone string, AES-GCM-encrypted."""
    async with session_factory() as s:
        row = await _get_or_create_row(s)
        row.enc_phone = _seal(dek, phone.encode("utf-8"))
        await s.commit()
    log.info("telegram_phone_saved")


async def load_phone(session_factory: Any, dek: bytes) -> str | None:
    """Return the saved phone string or ``None`` if not yet stored."""
    async with session_factory() as s:
        row = (
            await s.execute(select(UserSecret).where(UserSecret.id == 1))
        ).scalar_one_or_none()
        if row is None or row.enc_phone is None:
            return None
        return _unseal(dek, row.enc_phone).decode("utf-8")
