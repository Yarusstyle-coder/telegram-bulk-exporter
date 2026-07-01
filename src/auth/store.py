"""Encrypted on-disk store for TOTP secret and backup-code hashes.

Until Phase 2 lands SQLCipher, we stash a tiny JSON blob at
``{data_dir}/auth.json`` encrypted with the session's DEK via AES-256-GCM.

TODO(phase-2): migrate this into the SQLCipher `auth` table once the DB is
wired. Callers should go through `AuthStore` so migration is a drop-in change.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from src.crypto.aead import decrypt, encrypt
from src.logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class AuthData:
    """In-memory representation of what lives encrypted on disk."""

    totp_secret: str | None = None
    totp_enabled: bool = False
    backup_code_hashes: list[str] = field(default_factory=list)
    backup_codes_used: list[bool] = field(default_factory=list)
    setup_complete: bool = False

    def to_json(self) -> bytes:
        return json.dumps(asdict(self), sort_keys=True).encode("utf-8")

    @classmethod
    def from_json(cls, raw: bytes) -> AuthData:
        doc = json.loads(raw.decode("utf-8"))
        return cls(
            totp_secret=doc.get("totp_secret"),
            totp_enabled=bool(doc.get("totp_enabled", False)),
            backup_code_hashes=list(doc.get("backup_code_hashes", [])),
            backup_codes_used=list(doc.get("backup_codes_used", [])),
            setup_complete=bool(doc.get("setup_complete", False)),
        )


def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


class AuthStore:
    """Wraps read/write of the encrypted auth.json file."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self, dek: bytes) -> AuthData:
        if not self.exists():
            return AuthData()
        raw = self.path.read_bytes()
        env = json.loads(raw.decode("utf-8"))
        pt = decrypt(
            dek,
            _b64d(env["nonce"]),
            _b64d(env["ciphertext"]),
            _b64d(env["tag"]),
            associated=b"tge:auth",
        )
        return AuthData.from_json(pt)

    def save(self, dek: bytes, data: AuthData) -> None:
        env = encrypt(dek, data.to_json(), associated=b"tge:auth")
        payload = json.dumps(
            {
                "algorithm": "AES-256-GCM",
                "nonce": _b64e(env["nonce"]),
                "ciphertext": _b64e(env["ciphertext"]),
                "tag": _b64e(env["tag"]),
            },
            indent=2,
            sort_keys=True,
        ).encode("utf-8")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".auth-", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            Path(tmp_name).replace(self.path)
        except Exception:
            with contextlib.suppress(OSError):
                Path(tmp_name).unlink(missing_ok=True)
            raise
        log.info("auth_store_saved", path=str(self.path))
