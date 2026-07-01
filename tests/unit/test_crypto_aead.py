"""AES-256-GCM round-trip, authentication-failure and edge-case tests."""

from __future__ import annotations

import os

import pytest
from cryptography.exceptions import InvalidTag

from src.crypto.aead import decrypt, encrypt


def _key() -> bytes:
    return os.urandom(32)


def test_round_trip() -> None:
    key = _key()
    env = encrypt(key, b"hello world")
    pt = decrypt(key, env["nonce"], env["ciphertext"], env["tag"])
    assert pt == b"hello world"


def test_round_trip_with_aad() -> None:
    key = _key()
    env = encrypt(key, b"payload", associated=b"context")
    pt = decrypt(key, env["nonce"], env["ciphertext"], env["tag"], associated=b"context")
    assert pt == b"payload"


def test_round_trip_empty_plaintext() -> None:
    key = _key()
    env = encrypt(key, b"")
    assert env["ciphertext"] == b""
    pt = decrypt(key, env["nonce"], env["ciphertext"], env["tag"])
    assert pt == b""


def test_wrong_tag_fails() -> None:
    key = _key()
    env = encrypt(key, b"secret")
    bad_tag = bytes((env["tag"][0] ^ 0x01,)) + env["tag"][1:]
    with pytest.raises(InvalidTag):
        decrypt(key, env["nonce"], env["ciphertext"], bad_tag)


def test_wrong_nonce_fails() -> None:
    key = _key()
    env = encrypt(key, b"secret")
    bad_nonce = bytes((env["nonce"][0] ^ 0x01,)) + env["nonce"][1:]
    with pytest.raises(InvalidTag):
        decrypt(key, bad_nonce, env["ciphertext"], env["tag"])


def test_aad_mismatch_fails() -> None:
    key = _key()
    env = encrypt(key, b"secret", associated=b"good")
    with pytest.raises(InvalidTag):
        decrypt(key, env["nonce"], env["ciphertext"], env["tag"], associated=b"bad")


def test_wrong_key_fails() -> None:
    env = encrypt(_key(), b"secret")
    with pytest.raises(InvalidTag):
        decrypt(_key(), env["nonce"], env["ciphertext"], env["tag"])


def test_bad_key_length_rejected() -> None:
    with pytest.raises(ValueError):
        encrypt(b"\x00" * 16, b"x")
    with pytest.raises(ValueError):
        decrypt(b"\x00" * 16, b"\x00" * 12, b"", b"\x00" * 16)


def test_bad_nonce_length_rejected() -> None:
    with pytest.raises(ValueError):
        decrypt(_key(), b"\x00" * 8, b"", b"\x00" * 16)


def test_bad_tag_length_rejected() -> None:
    with pytest.raises(ValueError):
        decrypt(_key(), b"\x00" * 12, b"", b"\x00" * 8)
