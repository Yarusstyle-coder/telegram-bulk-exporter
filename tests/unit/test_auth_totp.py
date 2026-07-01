"""TOTP helpers — generate, verify, drift window, QR output."""

from __future__ import annotations

import pyotp

from src.auth.totp import new_secret, provisioning_uri, qr_png, verify


def test_new_secret_shape() -> None:
    s1 = new_secret()
    s2 = new_secret()
    assert s1 != s2
    # Base32 alphabet only.
    for ch in s1:
        assert ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
    assert len(s1) >= 16


def test_verify_correct_code() -> None:
    secret = new_secret()
    code = pyotp.TOTP(secret).now()
    assert verify(secret, code) is True


def test_verify_rejects_wrong_code() -> None:
    secret = new_secret()
    assert verify(secret, "000000") is False or verify(secret, "123456") is False


def test_verify_drift_plus_one() -> None:
    secret = new_secret()
    totp = pyotp.TOTP(secret)
    # Compute code for one step ahead.
    import time

    future_code = totp.at(int(time.time()) + 30)
    # With valid_window=1, code from the next step should be accepted.
    assert verify(secret, future_code, valid_window=1) is True


def test_verify_drift_minus_one() -> None:
    secret = new_secret()
    totp = pyotp.TOTP(secret)
    import time

    past_code = totp.at(int(time.time()) - 30)
    assert verify(secret, past_code, valid_window=1) is True


def test_verify_empty_and_garbage() -> None:
    secret = new_secret()
    assert verify(secret, "") is False
    assert verify(secret, "abcdef") is False
    assert verify(secret, "   ") is False


def test_verify_strips_whitespace_and_dashes() -> None:
    secret = new_secret()
    code = pyotp.TOTP(secret).now()
    assert verify(secret, f" {code[:3]} {code[3:]} ") is True


def test_provisioning_uri_shape() -> None:
    uri = provisioning_uri("JBSWY3DPEHPK3PXP", account="admin", issuer="MyApp")
    assert uri.startswith("otpauth://totp/")
    assert "MyApp" in uri
    assert "admin" in uri


def test_qr_png_returns_png_bytes() -> None:
    uri = provisioning_uri("JBSWY3DPEHPK3PXP", account="admin")
    png = qr_png(uri)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(png) > 100
