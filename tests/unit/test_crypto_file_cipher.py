"""Chunked AES-GCM file-cipher tests — edge sizes + tamper detection."""

from __future__ import annotations

import io
import os
from hashlib import sha256

import pytest

from src.crypto.file_cipher import FileCipher, FileCipherError


def _key() -> bytes:
    return os.urandom(32)


@pytest.mark.parametrize(
    "size",
    [0, 1, 1023, 1024, 1025, 10 * 1024 * 1024],
    ids=["0B", "1B", "chunk-1", "chunk", "chunk+1", "10MiB"],
)
def test_round_trip_various_sizes(size: int) -> None:
    key = _key()
    chunk_size = 1024  # small chunk for fast boundary tests
    src_bytes = os.urandom(size)

    enc_dst = io.BytesIO()
    FileCipher.encrypt_stream(
        key,
        io.BytesIO(src_bytes),
        enc_dst,
        chunk_size=chunk_size,
        header={"filename": "test.bin", "mime": "application/octet-stream"},
    )

    enc_dst.seek(0)
    dec_dst = io.BytesIO()
    header = FileCipher.decrypt_stream(key, enc_dst, dec_dst)

    assert header["filename"] == "test.bin"
    assert header["mime"] == "application/octet-stream"
    assert header["chunk_size"] == chunk_size
    assert dec_dst.getvalue() == src_bytes
    assert sha256(dec_dst.getvalue()).hexdigest() == sha256(src_bytes).hexdigest()


def test_default_chunk_size_round_trip_10mib() -> None:
    key = _key()
    src_bytes = os.urandom(10 * 1024 * 1024)
    enc_dst = io.BytesIO()
    FileCipher.encrypt_stream(key, io.BytesIO(src_bytes), enc_dst)
    enc_dst.seek(0)
    dec_dst = io.BytesIO()
    FileCipher.decrypt_stream(key, enc_dst, dec_dst)
    assert dec_dst.getvalue() == src_bytes


def test_header_preserved() -> None:
    key = _key()
    enc_dst = io.BytesIO()
    FileCipher.encrypt_stream(
        key,
        io.BytesIO(b"abc"),
        enc_dst,
        chunk_size=16,
        header={"filename": "x.txt", "mime": "text/plain", "extra": "stuff"},
    )
    enc_dst.seek(0)
    header = FileCipher.decrypt_stream(key, enc_dst, io.BytesIO())
    assert header["filename"] == "x.txt"
    assert header["mime"] == "text/plain"
    assert header["extra"] == "stuff"


def test_tampered_chunk_fails() -> None:
    key = _key()
    enc_dst = io.BytesIO()
    FileCipher.encrypt_stream(
        key, io.BytesIO(b"A" * 5000), enc_dst, chunk_size=1024
    )
    blob = bytearray(enc_dst.getvalue())
    # Flip a byte deep in the payload (past magic + lens + header + first nonce).
    blob[200] ^= 0x01
    with pytest.raises(FileCipherError):
        FileCipher.decrypt_stream(key, io.BytesIO(bytes(blob)), io.BytesIO())


def test_wrong_key_fails() -> None:
    enc_dst = io.BytesIO()
    FileCipher.encrypt_stream(
        _key(), io.BytesIO(b"payload"), enc_dst, chunk_size=32
    )
    enc_dst.seek(0)
    with pytest.raises(FileCipherError):
        FileCipher.decrypt_stream(_key(), enc_dst, io.BytesIO())


def test_bad_magic_rejected() -> None:
    buf = b"XXXX" + b"\x00" * 32
    with pytest.raises(FileCipherError):
        FileCipher.decrypt_stream(_key(), io.BytesIO(buf), io.BytesIO())


def test_truncated_file_detected() -> None:
    key = _key()
    enc_dst = io.BytesIO()
    FileCipher.encrypt_stream(
        key, io.BytesIO(os.urandom(3000)), enc_dst, chunk_size=1024
    )
    truncated = enc_dst.getvalue()[:-40]
    with pytest.raises(FileCipherError):
        FileCipher.decrypt_stream(key, io.BytesIO(truncated), io.BytesIO())
