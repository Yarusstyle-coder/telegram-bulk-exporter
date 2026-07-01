"""Thin Telethon wrapper — used for dialog metadata and avatars ONLY.

Media download is delegated to `tdl` because Telethon's transport is
significantly slower on bulk media. See `src/services/tdl_wrapper.py`.
"""

from src.telegram.telethon_client import (
    AuthStep,
    DialogEntry,
    TelegramAuthError,
    TelegramSessionManager,
)

__all__ = [
    "AuthStep",
    "DialogEntry",
    "TelegramAuthError",
    "TelegramSessionManager",
]
