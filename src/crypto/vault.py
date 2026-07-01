"""Vault file ops — KEK+DEK envelope stored as JSON.

Schema (v1)::

    {
      "version": 1,
      "created_at": iso8601,
      "kdf": {
        "algorithm": "argon2id",
        "memory_cost": int,
        "time_cost": int,
        "parallelism": int,
        "salt": b64
      },
      "encrypted_dek": {
        "algorithm": "AES-256-GCM",
        "nonce": b64, "ciphertext": b64, "tag": b64
      },
      "verification": {
        "algorithm": "AES-256-GCM",
        "nonce": b64, "ciphertext": b64, "tag": b64
      }
    }

The verification envelope encrypts a fixed plaintext with the DEK so we can
distinguish "wrong password" from "corrupted file".
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import secrets
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from cryptography.exceptions import InvalidTag

from src.crypto.aead import decrypt, encrypt
from src.crypto.kdf import (
    MEMORY_COST,
    PARALLELISM,
    TIME_COST,
    derive_kek,
    new_salt,
)
from src.logging_setup import get_logger

log = get_logger(__name__)

VAULT_VERSION = 1
VAULT_VERIFICATION_PLAINTEXT = b"TGEXPORT_OK"
DEK_LEN = 32


class InvalidPassword(Exception):
    """Raised when a password fails to decrypt the DEK / verification blob."""


def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


def vault_exists(path: Path) -> bool:
    """Return True iff a vault file is present at `path`."""
    return Path(path).is_file()


def _write_atomic(path: Path, payload: bytes) -> None:
    """Write `payload` to `path` atomically via temp-file + `os.replace`."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # NamedTemporaryFile in same dir so os.replace is a same-filesystem move.
    fd, tmp_name = tempfile.mkstemp(prefix=".vault-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        Path(tmp_name).replace(path)
    except Exception:
        with contextlib.suppress(OSError):
            Path(tmp_name).unlink(missing_ok=True)
        raise


def _encode_vault(
    *,
    salt: bytes,
    memory_cost: int,
    time_cost: int,
    parallelism: int,
    enc_dek: dict[str, bytes],
    enc_verify: dict[str, bytes],
) -> bytes:
    doc = {
        "version": VAULT_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "kdf": {
            "algorithm": "argon2id",
            "memory_cost": memory_cost,
            "time_cost": time_cost,
            "parallelism": parallelism,
            "salt": _b64e(salt),
        },
        "encrypted_dek": {
            "algorithm": "AES-256-GCM",
            "nonce": _b64e(enc_dek["nonce"]),
            "ciphertext": _b64e(enc_dek["ciphertext"]),
            "tag": _b64e(enc_dek["tag"]),
        },
        "verification": {
            "algorithm": "AES-256-GCM",
            "nonce": _b64e(enc_verify["nonce"]),
            "ciphertext": _b64e(enc_verify["ciphertext"]),
            "tag": _b64e(enc_verify["tag"]),
        },
    }
    return json.dumps(doc, indent=2, sort_keys=True).encode("utf-8")


def create_vault(
    password: str,
    path: Path,
    *,
    memory_cost: int = MEMORY_COST,
    time_cost: int = TIME_COST,
    parallelism: int = PARALLELISM,
) -> bytes:
    """Create a fresh vault at `path`. Returns the raw DEK.

    Overwrites any existing file at `path` (atomic).
    """
    path = Path(path)
    salt = new_salt()
    kek = derive_kek(
        password,
        salt,
        memory_cost=memory_cost,
        time_cost=time_cost,
        parallelism=parallelism,
    )
    dek = secrets.token_bytes(DEK_LEN)
    enc_dek = encrypt(kek, dek, associated=b"tge:dek")
    enc_verify = encrypt(dek, VAULT_VERIFICATION_PLAINTEXT, associated=b"tge:verify")

    payload = _encode_vault(
        salt=salt,
        memory_cost=memory_cost,
        time_cost=time_cost,
        parallelism=parallelism,
        enc_dek=enc_dek,
        enc_verify=enc_verify,
    )
    _write_atomic(path, payload)
    log.info("vault_created", path=str(path))
    return dek


def _load_vault(path: Path) -> dict:
    raw = Path(path).read_bytes()
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("vault file is corrupted (not valid JSON)") from exc
    if not isinstance(doc, dict) or doc.get("version") != VAULT_VERSION:
        raise ValueError("vault file has unsupported version")
    for key in ("kdf", "encrypted_dek", "verification"):
        if key not in doc:
            raise ValueError(f"vault file missing section: {key}")
    return doc


def unlock_vault(password: str, path: Path) -> bytes:
    """Unlock the vault at `path` with `password`. Returns the raw DEK.

    Raises `InvalidPassword` on wrong password, `ValueError` on corrupt file,
    `FileNotFoundError` on missing file.
    """
    doc = _load_vault(path)
    kdf = doc["kdf"]
    if kdf.get("algorithm") != "argon2id":
        raise ValueError("vault uses unsupported KDF algorithm")
    salt = _b64d(kdf["salt"])
    kek = derive_kek(
        password,
        salt,
        memory_cost=int(kdf["memory_cost"]),
        time_cost=int(kdf["time_cost"]),
        parallelism=int(kdf["parallelism"]),
    )

    enc_dek = doc["encrypted_dek"]
    try:
        dek = decrypt(
            kek,
            _b64d(enc_dek["nonce"]),
            _b64d(enc_dek["ciphertext"]),
            _b64d(enc_dek["tag"]),
            associated=b"tge:dek",
        )
    except InvalidTag as exc:
        raise InvalidPassword("password did not unlock the vault") from exc

    enc_verify = doc["verification"]
    try:
        verify = decrypt(
            dek,
            _b64d(enc_verify["nonce"]),
            _b64d(enc_verify["ciphertext"]),
            _b64d(enc_verify["tag"]),
            associated=b"tge:verify",
        )
    except InvalidTag as exc:
        raise ValueError("vault verification blob is corrupted") from exc
    if verify != VAULT_VERIFICATION_PLAINTEXT:
        raise ValueError("vault verification plaintext mismatch")

    return dek


def change_password(
    old: str,
    new: str,
    path: Path,
    *,
    memory_cost: int = MEMORY_COST,
    time_cost: int = TIME_COST,
    parallelism: int = PARALLELISM,
) -> None:
    """Rewrap the DEK with a KEK derived from `new`. DEK itself is unchanged.

    Atomic write — either the whole vault is swapped or nothing changes.
    """
    path = Path(path)
    dek = unlock_vault(old, path)
    salt = new_salt()
    new_kek = derive_kek(
        new,
        salt,
        memory_cost=memory_cost,
        time_cost=time_cost,
        parallelism=parallelism,
    )
    enc_dek = encrypt(new_kek, dek, associated=b"tge:dek")
    enc_verify = encrypt(dek, VAULT_VERIFICATION_PLAINTEXT, associated=b"tge:verify")
    payload = _encode_vault(
        salt=salt,
        memory_cost=memory_cost,
        time_cost=time_cost,
        parallelism=parallelism,
        enc_dek=enc_dek,
        enc_verify=enc_verify,
    )
    _write_atomic(path, payload)
    log.info("vault_password_changed", path=str(path))
