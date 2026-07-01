"""Chunked AES-256-GCM file cipher — `.tge` container format.

Layout::

    magic(4)         = b"TGE1"
    chunk_size(4 LE) = u32    (plaintext bytes per chunk)
    header_len(4 LE) = u32    (bytes of JSON header that follows)
    header_json      = header_len bytes of UTF-8 JSON
    [ nonce(12) | ct_len(4 LE) | ciphertext+tag(ct_len bytes) ] * repeated...

Each chunk carries its own random nonce and an explicit length prefix (so we
can find the chunk boundary without reading past it).  AAD for chunk `i` is the
big-endian 8-byte index, preventing chunk-reordering attacks.  The stream ends
with a chunk whose plaintext is empty (ct_len == 16, holding only the GCM tag),
so a truncated file is always detectable.
"""

from __future__ import annotations

import json
import secrets
import struct
from typing import BinaryIO

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"TGE1"
NONCE_LEN = 12
TAG_LEN = 16
DEFAULT_CHUNK_SIZE = 1024 * 1024  # 1 MiB


class FileCipherError(Exception):
    """Raised on malformed container or auth failure."""


class FileCipher:
    """Streaming encrypt/decrypt of large files with chunked AES-GCM."""

    @staticmethod
    def encrypt_stream(
        key: bytes,
        src: BinaryIO,
        dst: BinaryIO,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        header: dict | None = None,
    ) -> None:
        """Encrypt `src` into `dst` as a `.tge` container."""
        if len(key) != 32:
            raise ValueError("key must be 32 bytes")
        if chunk_size <= 0 or chunk_size > 0xFFFFFFFF:
            raise ValueError("chunk_size out of range")

        hdr = dict(header) if header else {}
        hdr.setdefault("v", 1)
        hdr.setdefault("chunk_size", chunk_size)
        header_bytes = json.dumps(hdr, sort_keys=True).encode("utf-8")

        dst.write(MAGIC)
        dst.write(struct.pack("<I", chunk_size))
        dst.write(struct.pack("<I", len(header_bytes)))
        dst.write(header_bytes)

        aead = AESGCM(key)
        index = 0
        while True:
            plaintext = src.read(chunk_size)
            if not plaintext:
                # Write terminating empty-chunk marker so truncation is detectable.
                _write_chunk(dst, aead, index, b"")
                break

            _write_chunk(dst, aead, index, plaintext)
            index += 1

            if len(plaintext) < chunk_size:
                # Source exhausted on this chunk — still append terminator.
                _write_chunk(dst, aead, index, b"")
                break

    @staticmethod
    def decrypt_stream(key: bytes, src: BinaryIO, dst: BinaryIO) -> dict:
        """Decrypt a `.tge` container from `src` into `dst`. Returns the header dict."""
        if len(key) != 32:
            raise ValueError("key must be 32 bytes")

        magic = _read_exact(src, 4)
        if magic != MAGIC:
            raise FileCipherError("bad magic - not a .tge file")
        chunk_size = struct.unpack("<I", _read_exact(src, 4))[0]
        header_len = struct.unpack("<I", _read_exact(src, 4))[0]
        if header_len > 1_048_576:
            raise FileCipherError("implausibly large header")
        header_bytes = _read_exact(src, header_len)
        try:
            header = json.loads(header_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise FileCipherError("header is not valid JSON") from exc
        if not isinstance(header, dict):
            raise FileCipherError("header JSON is not an object")

        aead = AESGCM(key)
        index = 0
        while True:
            nonce = src.read(NONCE_LEN)
            if len(nonce) == 0:
                raise FileCipherError("unexpected EOF - missing terminator")
            if len(nonce) < NONCE_LEN:
                raise FileCipherError("short nonce - file truncated")

            ct_len_bytes = src.read(4)
            if len(ct_len_bytes) < 4:
                raise FileCipherError("short chunk length - file truncated")
            ct_len = struct.unpack("<I", ct_len_bytes)[0]
            # Bound to chunk_size + tag so a corrupt length can't make us allocate
            # wildly.
            if ct_len < TAG_LEN or ct_len > chunk_size + TAG_LEN:
                raise FileCipherError(f"implausible chunk length {ct_len}")

            payload = src.read(ct_len)
            if len(payload) < ct_len:
                raise FileCipherError("short payload - file truncated")
            aad = _aad_for(index)
            try:
                pt = aead.decrypt(nonce, payload, aad)
            except InvalidTag as exc:
                raise FileCipherError(
                    f"authentication failed on chunk {index}"
                ) from exc
            if pt == b"":
                # Terminator reached.
                return header
            dst.write(pt)
            index += 1


def _write_chunk(dst: BinaryIO, aead: AESGCM, index: int, plaintext: bytes) -> None:
    nonce = secrets.token_bytes(NONCE_LEN)
    aad = _aad_for(index)
    ct = aead.encrypt(nonce, plaintext, aad)
    dst.write(nonce)
    dst.write(struct.pack("<I", len(ct)))
    dst.write(ct)


def _aad_for(index: int) -> bytes:
    return index.to_bytes(8, "big")


def _read_exact(src: BinaryIO, n: int) -> bytes:
    buf = src.read(n)
    if len(buf) != n:
        raise FileCipherError(f"expected {n} bytes, got {len(buf)}")
    return buf
