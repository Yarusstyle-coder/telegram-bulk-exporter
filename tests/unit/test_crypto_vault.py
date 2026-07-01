"""Vault create/unlock/change-password round-trip tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.crypto.vault import (
    InvalidPassword,
    change_password,
    create_vault,
    unlock_vault,
    vault_exists,
)

FAST = {"memory_cost": 8192, "time_cost": 1, "parallelism": 1}


def test_create_and_unlock_round_trip(tmp_path: Path) -> None:
    vault = tmp_path / "vault.json"
    assert not vault_exists(vault)

    dek = create_vault("correct-horse-battery-staple", vault, **FAST)
    assert vault_exists(vault)
    assert len(dek) == 32

    unlocked = unlock_vault("correct-horse-battery-staple", vault)
    assert unlocked == dek


def test_wrong_password_raises_invalid_password(tmp_path: Path) -> None:
    vault = tmp_path / "vault.json"
    create_vault("correct", vault, **FAST)
    with pytest.raises(InvalidPassword):
        unlock_vault("wrong", vault)


def test_change_password_rolls(tmp_path: Path) -> None:
    vault = tmp_path / "vault.json"
    dek = create_vault("old-pw-12345", vault, **FAST)

    change_password("old-pw-12345", "new-pw-67890", vault, **FAST)

    with pytest.raises(InvalidPassword):
        unlock_vault("old-pw-12345", vault)

    dek_after = unlock_vault("new-pw-67890", vault)
    # DEK unchanged — only the KEK-wrapping was rotated.
    assert dek_after == dek


def test_change_password_requires_correct_old(tmp_path: Path) -> None:
    vault = tmp_path / "vault.json"
    create_vault("old", vault, **FAST)
    with pytest.raises(InvalidPassword):
        change_password("bogus", "new", vault, **FAST)


def test_corrupted_file_raises(tmp_path: Path) -> None:
    vault = tmp_path / "vault.json"
    create_vault("correct", vault, **FAST)
    vault.write_bytes(b"not valid json at all {")
    with pytest.raises(ValueError):
        unlock_vault("correct", vault)


def test_tampered_ciphertext_raises(tmp_path: Path) -> None:
    import json

    vault = tmp_path / "vault.json"
    create_vault("correct", vault, **FAST)
    doc = json.loads(vault.read_text())
    # Flip a bit in the DEK ciphertext.
    import base64

    ct = bytearray(base64.b64decode(doc["encrypted_dek"]["ciphertext"]))
    if len(ct) == 0:
        ct.append(0)
    ct[0] ^= 0x01
    doc["encrypted_dek"]["ciphertext"] = base64.b64encode(bytes(ct)).decode("ascii")
    vault.write_text(json.dumps(doc))

    with pytest.raises(InvalidPassword):
        unlock_vault("correct", vault)


def test_missing_file_raises(tmp_path: Path) -> None:
    vault = tmp_path / "nope.json"
    assert not vault_exists(vault)
    with pytest.raises(FileNotFoundError):
        unlock_vault("x", vault)
