"""Unit tests for the proxy URL parser."""

from __future__ import annotations

import pytest

from src.telegram.proxy import parse_proxy


def test_none_and_empty_return_none() -> None:
    assert parse_proxy(None) is None
    assert parse_proxy("") is None
    assert parse_proxy("   ") is None


def test_socks5_basic() -> None:
    p = parse_proxy("socks5://1.2.3.4:1080")
    assert p is not None
    assert p.kind == "socks5"
    assert p.host == "1.2.3.4"
    assert p.port == 1080
    assert p.username is None
    assert p.to_tdl_url() == "socks5://1.2.3.4:1080"


def test_socks5_with_credentials() -> None:
    p = parse_proxy("socks5://alice:s3cret@host:1080")
    assert p is not None
    assert p.username == "alice"
    assert p.password == "s3cret"
    assert p.to_tdl_url() == "socks5://alice:s3cret@host:1080"


def test_http_proxy() -> None:
    p = parse_proxy("http://corp.proxy:8080")
    assert p is not None and p.kind == "http"


def test_socks5h_for_remote_dns() -> None:
    p = parse_proxy("socks5h://host:1080")
    assert p is not None and p.kind == "socks5h"


def test_mtproto_with_hex_secret() -> None:
    p = parse_proxy(
        "mtproto://203.0.113.10:443?secret=ee0102030405060708090a0b0c0d0e0f"
    )
    assert p is not None
    assert p.kind == "mtproto"
    assert p.host == "203.0.113.10"
    assert p.port == 443
    assert p.secret_hex == "ee0102030405060708090a0b0c0d0e0f"
    # tdl can't carry MTProto:
    assert p.to_tdl_url() is None


def test_mtproxy_alias() -> None:
    p = parse_proxy("mtproxy://h:443?secret=" + "ab" * 16)
    assert p is not None and p.kind == "mtproto"


def test_mtproto_with_base64_secret() -> None:
    p = parse_proxy(
        "mtproto://h:1?secret=7gECAwQFBgcICQoLDA0ODxBpLmV4YW1wbGUuY29t"
    )
    assert p is not None and p.kind == "mtproto"
    # All-hex result, no padding chars left
    assert all(c in "0123456789abcdef" for c in p.secret_hex or "")
    # Telethon needs exactly 16 bytes (32 hex chars).
    assert len(p.secret_hex or "") == 32


def test_mtproto_strips_faketls_ee_prefix_and_sni_suffix() -> None:
    """A fake-TLS secret has `ee` + 16 random bytes + ASCII SNI domain.
    We keep only the 16-byte core so Telethon can speak random-padded mode."""
    core = "11" * 16
    sni = "userver.yandex.com".encode("ascii").hex()
    secret_hex = "ee" + core + sni
    p = parse_proxy(f"mtproto://h:443?secret={secret_hex}")
    assert p is not None
    assert p.secret_hex == core
    assert len(p.secret_hex) == 32


def test_mtproto_strips_random_padded_dd_prefix() -> None:
    core = "ab" * 16
    secret_hex = "dd" + core
    p = parse_proxy(f"mtproto://h:443?secret={secret_hex}")
    assert p is not None
    assert p.secret_hex == core


def test_mtproto_legacy_16_byte_kept_as_is() -> None:
    # Legacy 16-byte secret may happen to start with `0xee`. Keep it as-is —
    # there is no SNI suffix to strip (unlike a fake-TLS `ee`+core+SNI secret).
    secret = "ee0102030405060708090a0b0c0d0e0f"
    assert len(secret) == 32  # 16 bytes
    p = parse_proxy(f"mtproto://h:443?secret={secret}")
    assert p is not None
    assert p.secret_hex == secret


def test_telegram_share_link() -> None:
    url = "https://t.me/proxy?server=203.0.113.10&port=443&secret=ee0102030405060708090a0b0c0d0e0f"
    p = parse_proxy(url)
    assert p is not None
    assert p.kind == "mtproto"
    assert p.host == "203.0.113.10"
    assert p.port == 443
    assert p.secret_hex == "ee0102030405060708090a0b0c0d0e0f"


def test_tg_proxy_link() -> None:
    p = parse_proxy("tg://proxy?server=h&port=99&secret=" + "cd" * 16)
    assert p is not None and p.kind == "mtproto" and p.port == 99


@pytest.mark.parametrize(
    "url,reason",
    [
        ("foobar://h:1", "scheme"),
        ("mtproto://h:443", "secret"),
        ("https://t.me/proxy?server=h&port=80", "secret"),
        ("socks5://only-host", "host"),
    ],
)
def test_invalid_urls_raise(url: str, reason: str) -> None:
    with pytest.raises(ValueError):
        parse_proxy(url)


def test_to_telethon_socks() -> None:
    p = parse_proxy("socks5://h:1080")
    assert p is not None
    conn, tup = p.to_telethon()
    assert conn is None
    assert tup[1] == "h" and tup[2] == 1080


def test_to_telethon_mtproto() -> None:
    p = parse_proxy("mtproto://h:443?secret=" + "ee" * 16)
    assert p is not None
    conn, tup = p.to_telethon()
    assert conn is not None  # ConnectionTcpMTProxyRandomizedIntermediate
    assert tup[0] == "h" and tup[1] == 443
    assert tup[2] == "ee" * 16
