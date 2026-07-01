"""Parse proxy URLs into Telethon-friendly proxy descriptors.

Telethon's `proxy=` argument has several flavours:

- SOCKS5/4/HTTP via `python-socks` / PySocks tuples:
      (socks.SOCKS5, host, port[, rdns, username, password])
- MTProto via `(host, port, secret_hex)` plus `connection=` set to one of
  the `ConnectionTcpMTProxy*` classes from `telethon.connection`.

This module accepts any of these URL styles and returns a small dict
that the wrapper consumes:

    {"telethon_proxy": tuple, "telethon_connection": class | None}

Supported URL forms:

- `socks5://[user:pass@]host:port`
- `socks5h://[user:pass@]host:port`
- `socks4://...`
- `http://[user:pass@]host:port`
- `https://[user:pass@]host:port`
- `mtproto://host:port?secret=HEX_OR_BASE64`
- `mtproxy://host:port?secret=...`     (alias of `mtproto://`)
- Telegram share link: `https://t.me/proxy?server=HOST&port=PORT&secret=...`
- `tg://proxy?server=HOST&port=PORT&secret=...`

The MTProto secret may be hex (32 chars) or padded hex with `ee`/`dd` prefixes
(faketls / random padded). Base64 secrets (Telegram clients sometimes show
them) are also accepted and converted to hex.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


@dataclass(slots=True)
class ParsedProxy:
    kind: str  # "socks5" | "socks5h" | "socks4" | "http" | "https" | "mtproto"
    host: str
    port: int
    username: str | None = None
    password: str | None = None
    secret_hex: str | None = None  # for MTProto

    def to_telethon(self) -> tuple[Any | None, tuple]:
        """Return `(connection_class_or_None, proxy_tuple)` for Telethon."""
        if self.kind == "mtproto":
            from telethon.network.connection import (  # type: ignore[import-not-found]
                ConnectionTcpMTProxyRandomizedIntermediate,
            )

            assert self.secret_hex is not None
            return (
                ConnectionTcpMTProxyRandomizedIntermediate,
                (self.host, self.port, self.secret_hex),
            )

        # SOCKS / HTTP via python-socks
        import socks  # type: ignore[import-not-found]

        kind_map = {
            "socks5": socks.SOCKS5,
            "socks5h": socks.SOCKS5,
            "socks4": socks.SOCKS4,
            "http": socks.HTTP,
            "https": socks.HTTP,
        }
        proxy_type = kind_map[self.kind]
        rdns = self.kind == "socks5h"
        if self.username is not None:
            return None, (
                proxy_type, self.host, self.port, rdns, self.username, self.password
            )
        return None, (proxy_type, self.host, self.port, rdns)

    def to_tdl_url(self) -> str | None:
        """Return a tdl-compatible proxy URL or None if not representable.

        tdl supports SOCKS5 / SOCKS5H and HTTP CONNECT only — no MTProto.
        """
        if self.kind == "mtproto":
            return None
        auth = ""
        if self.username:
            auth = f"{self.username}:{self.password or ''}@"
        return f"{self.kind}://{auth}{self.host}:{self.port}"


_HEX = re.compile(r"^[0-9a-fA-F]+$")


def _normalise_secret(raw: str) -> str:
    """Coerce a Telegram MTProto secret into the 16-byte hex form Telethon wants.

    Real-world Telegram secret formats:

    - **Legacy** (16 raw bytes, 32 hex chars). Used by old MTProxy. Pass
      through as-is.
    - **Random-padded** (`dd` + 16 bytes = 17 bytes / 34 hex). The first byte
      is a marker; the actual key is the next 16. We strip the marker.
    - **Fake-TLS** (`ee` + 16 bytes + ASCII SNI suffix = 17+N bytes). Clients
      typically surface this as base64 with the SNI padding included. Telethon
      doesn't speak fake-TLS, so we keep the 16-byte core and let
      `ConnectionTcpMTProxyRandomizedIntermediate` carry the traffic — most
      fake-TLS-capable servers also accept random-padded connections, so this
      works in practice.

    Inputs accepted: hex (any case) **or** base64 / urlsafe-base64.

    Returns the canonical lowercase 32-char hex string, or raises ValueError.
    """
    s = raw.strip()
    if not s:
        raise ValueError("empty MTProto secret")

    raw_bytes: bytes
    if _HEX.match(s) and len(s) % 2 == 0:
        raw_bytes = bytes.fromhex(s)
    else:
        # Try base64 (urlsafe, w/ or w/o padding).
        decoded: bytes | None = None
        pad = "=" * (-len(s) % 4)
        for decoder in (base64.urlsafe_b64decode, base64.b64decode):
            try:
                decoded = decoder(s + pad)
                break
            except Exception:  # noqa: BLE001
                continue
        if decoded is None:
            raise ValueError(f"unrecognised MTProto secret format: {raw!r}")
        raw_bytes = decoded

    # Strip the marker / SNI padding.
    if len(raw_bytes) == 16:
        core = raw_bytes
    elif len(raw_bytes) >= 17 and raw_bytes[0] in (0xEE, 0xDD):
        # Fake-TLS / random-padded prefix → next 16 bytes are the real key.
        core = raw_bytes[1:17]
    elif len(raw_bytes) > 16:
        # Unknown trailing data; take the first 16 bytes and hope.
        core = raw_bytes[:16]
    else:
        raise ValueError(
            f"MTProto secret too short: got {len(raw_bytes)} bytes, need >=16 ({raw!r})"
        )

    return core.hex()


def parse_proxy(url: str | None) -> ParsedProxy | None:
    """Parse a proxy URL string. Returns None for empty / None input.

    Raises ValueError for malformed URLs."""
    if not url:
        return None
    u = url.strip()
    if not u:
        return None

    # Convert Telegram share-link (`https://t.me/proxy?...`) and `tg://proxy?...`
    # into our canonical `mtproto://...` first.
    if u.startswith("https://t.me/proxy?") or u.startswith("http://t.me/proxy?") or u.startswith("tg://proxy?"):
        q = parse_qs(urlparse(u).query)
        server = (q.get("server") or [""])[0]
        port_str = (q.get("port") or [""])[0]
        secret = (q.get("secret") or [""])[0]
        if not server or not port_str or not secret:
            raise ValueError(f"telegram-style proxy link missing fields: {url!r}")
        return ParsedProxy(
            kind="mtproto",
            host=server,
            port=int(port_str),
            secret_hex=_normalise_secret(unquote(secret)),
        )

    parsed = urlparse(u)
    scheme = (parsed.scheme or "").lower()
    if not scheme:
        raise ValueError(f"missing scheme in proxy URL: {url!r}")

    host = parsed.hostname
    port = parsed.port
    if not host or not port:
        raise ValueError(f"proxy URL missing host or port: {url!r}")

    if scheme in ("mtproto", "mtproxy"):
        q = parse_qs(parsed.query)
        secret = (q.get("secret") or [""])[0]
        if not secret:
            raise ValueError("MTProto proxy URL missing secret")
        return ParsedProxy(
            kind="mtproto",
            host=host,
            port=port,
            secret_hex=_normalise_secret(unquote(secret)),
        )

    if scheme in ("socks5", "socks5h", "socks4", "http", "https"):
        return ParsedProxy(
            kind=scheme,
            host=host,
            port=port,
            username=parsed.username,
            password=parsed.password,
        )

    raise ValueError(f"unsupported proxy scheme {scheme!r} (try socks5/http/mtproto)")
