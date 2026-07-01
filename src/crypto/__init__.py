"""Crypto primitives: KDF, AEAD, vault, chunked file cipher, memory helpers."""

from __future__ import annotations

from src.crypto.aead import decrypt, encrypt
from src.crypto.file_cipher import FileCipher
from src.crypto.kdf import (
    HASH_LEN,
    MEMORY_COST,
    PARALLELISM,
    SALT_LEN,
    TIME_COST,
    derive_kek,
    new_salt,
)
from src.crypto.memory import secure_zero
from src.crypto.vault import (
    VAULT_VERIFICATION_PLAINTEXT,
    InvalidPassword,
    change_password,
    create_vault,
    unlock_vault,
    vault_exists,
)

__all__ = [
    "HASH_LEN",
    "MEMORY_COST",
    "PARALLELISM",
    "SALT_LEN",
    "TIME_COST",
    "VAULT_VERIFICATION_PLAINTEXT",
    "FileCipher",
    "InvalidPassword",
    "change_password",
    "create_vault",
    "decrypt",
    "derive_kek",
    "encrypt",
    "new_salt",
    "secure_zero",
    "unlock_vault",
    "vault_exists",
]
