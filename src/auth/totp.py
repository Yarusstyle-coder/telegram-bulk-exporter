"""TOTP (RFC 6238) helpers — secret generation, provisioning URI, QR PNG, verify."""

from __future__ import annotations

import io

import pyotp
import segno


def new_secret() -> str:
    """Return a random 160-bit base32 secret (20 bytes / 32 chars)."""
    return pyotp.random_base32(length=32)


def provisioning_uri(
    secret: str,
    account: str,
    issuer: str = "TelegramExporter",
) -> str:
    """otpauth:// URI for feeding into authenticator apps."""
    return pyotp.TOTP(secret).provisioning_uri(name=account, issuer_name=issuer)


def qr_png(uri: str) -> bytes:
    """Render `uri` to a PNG QR code (bytes)."""
    qr = segno.make(uri, error="m")
    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=6, border=2)
    return buf.getvalue()


def verify(secret: str, code: str, valid_window: int = 1) -> bool:
    """Verify `code` against `secret`. `valid_window` allows clock drift ±N steps."""
    if not code:
        return False
    normalized = code.strip().replace(" ", "").replace("-", "")
    if not normalized.isdigit() or len(normalized) not in (6, 7, 8):
        return False
    return bool(pyotp.TOTP(secret).verify(normalized, valid_window=valid_window))
