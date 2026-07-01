"""Application configuration loaded from environment / .env."""

from __future__ import annotations

import sys
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# tdl ships two binary names depending on platform: `tdl.exe` on Windows,
# bare `tdl` everywhere else (Linux/macOS builds have no extension). This
# is only the *default* — an explicit TDL_BINARY_PATH env var always wins
# via pydantic-settings' normal precedence.
_DEFAULT_TDL_BINARY = "./tools/tdl/tdl.exe" if sys.platform == "win32" else "./tools/tdl/tdl"


class Settings(BaseSettings):
    """Environment-driven settings.

    Values come from (in order of precedence): actual env vars, `.env` in cwd,
    defaults below.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Telegram
    telegram_api_id: int | None = None
    telegram_api_hash: str | None = None

    # tdl
    tdl_binary_path: str = _DEFAULT_TDL_BINARY
    tdl_namespace: str = "default"
    # Override tdl's default ~/.tdl/data/<ns> bolt storage by writing to a
    # path under our DATA_DIR. Keeps every persisted byte under the
    # project directory (and the user's preferred drive).
    tdl_use_local_storage: bool = True

    # Storage
    export_dir: Path = Path("./exports")
    data_dir: Path = Path("./data")

    # Web
    web_host: str = "127.0.0.1"
    web_port: int = 8765

    # Logging
    log_level: str = "INFO"

    # Security / sessions
    # 0 = never auto-lock (permanent local session). Set to a positive
    # number of seconds to enforce a re-login window. Default keeps you
    # signed in for a single-user dev box.
    auto_lock_seconds: int = 0
    session_cookie_name: str = "tge_session"
    # When True, the session DEK is stored in the OS secret store (DPAPI /
    # Keychain / libsecret) so the server can restore the session on
    # restart without prompting for the master password. Set false for
    # maximum safety on a multi-user box.
    persist_sessions: bool = True

    # Dedup
    dedup_enabled: bool = True

    # Proxy — single URL (legacy) and a comma/semicolon-separated pool.
    # Effective pool = `proxies` ∪ (`proxy` if set) ∪ entries added via UI.
    proxy: str | None = None
    proxies: str | None = None  # comma- or semicolon-separated list
    proxy_auto_select: bool = True  # at startup, ping & pick the fastest
    proxy_test_interval_seconds: int = 1800  # re-measure every 30 min

    # Derived paths — resolved lazily
    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def avatars_dir(self) -> Path:
        return self.data_dir / "avatars"

    @property
    def vault_path(self) -> Path:
        return self.data_dir / "vault.json"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "state.db"

    @property
    def media_pool_dir(self) -> Path:
        return self.data_dir / "media_pool"

    @property
    def proxy_pool_path(self) -> Path:
        return self.data_dir / "proxy_pool.json"

    @property
    def tdl_storage_dir(self) -> Path:
        """Where tdl's bolt-DB / file storage lives when we override it."""
        return (self.data_dir / "tdl").resolve()

    def env_proxy_seed(self) -> list[str]:
        """Return proxy URLs declared via env (`PROXY` and/or `PROXIES`)."""
        seed: list[str] = []
        if self.proxy:
            seed.append(self.proxy.strip())
        if self.proxies:
            for chunk in self.proxies.replace(";", ",").split(","):
                chunk = chunk.strip()
                if chunk:
                    seed.append(chunk)
        # de-dupe while preserving order
        seen: set[str] = set()
        out: list[str] = []
        for u in seed:
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out

    def ensure_dirs(self) -> None:
        """Create all required directories with safe permissions."""
        for p in (
            self.data_dir,
            self.export_dir,
            self.sessions_dir,
            self.avatars_dir,
            self.media_pool_dir,
        ):
            p.mkdir(parents=True, exist_ok=True)

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return a cached Settings singleton."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings_cache() -> None:
    """Test helper — clear the cached singleton."""
    global _settings
    _settings = None
