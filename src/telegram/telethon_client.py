"""Telethon client wrapper.

Responsibilities:

- Walk the user through the phone+code (+2FA) login flow.
- Cache the Telethon `.session` file, AES-GCM-encrypted on disk.
- Enumerate dialogs (`iter_dialogs`) with human-readable metadata.
- Download profile photos for UI avatars.

Session encryption
------------------
Telethon persists state in a SQLite file. To store it encrypted we load it
into memory via StringSession (ASCII-safe base64), then pass through our
`crypto.aead.encrypt/decrypt` helpers with the current session DEK. The
resulting `<name>.tgsess` file on disk looks like:

    {"nonce": b64, "ciphertext": b64, "tag": b64}

When the user locks the UI session we zero the StringSession from memory.
"""

from __future__ import annotations

import base64
import contextlib
import enum
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession
from telethon.tl.types import (
    Channel,
    User,
)
from telethon.tl.types import (
    Chat as TlChat,
)

from src.crypto.aead import decrypt as aead_decrypt
from src.crypto.aead import encrypt as aead_encrypt
from src.db.models import ChatType
from src.logging_setup import get_logger

log = get_logger(__name__)


class AuthStep(str, enum.Enum):
    PHONE = "phone"
    CODE = "code"
    PASSWORD = "password"  # 2FA
    READY = "ready"


class TelegramAuthError(Exception):
    """User-facing auth error carrying a short reason code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class DialogEntry:
    id: int
    title: str
    username: str | None
    type: ChatType
    is_archived: bool
    approx_message_count: int | None
    last_message_date: Any  # datetime | None
    first_name: str | None = None
    last_name: str | None = None
    is_public: bool = False  # has @username
    folder_ids: list[int] = field(default_factory=list)


@dataclass(slots=True)
class DialogFolder:
    id: int
    title: str
    chat_ids: list[int]


def _dialog_type(entity: Any) -> ChatType:
    if isinstance(entity, User):
        return ChatType.BOT if getattr(entity, "bot", False) else ChatType.PRIVATE
    if isinstance(entity, Channel):
        return ChatType.CHANNEL if getattr(entity, "broadcast", False) else ChatType.SUPERGROUP
    if isinstance(entity, TlChat):
        return ChatType.GROUP
    return ChatType.UNKNOWN


def _readable_name(entity: Any) -> str:
    """Compose the user-visible label for a dialog. Falls back through:
    title → first/last name → username → numeric id."""
    title = getattr(entity, "title", None)
    if title:
        return str(title)
    first = (getattr(entity, "first_name", None) or "").strip()
    last = (getattr(entity, "last_name", None) or "").strip()
    if first or last:
        return f"{first} {last}".strip()
    username = getattr(entity, "username", None)
    if username:
        return f"@{username}"
    return str(getattr(entity, "id", "?"))


class TelegramSessionManager:
    """Own the Telethon client lifecycle for a single user session."""

    def __init__(
        self,
        *,
        api_id: int,
        api_hash: str,
        session_path: Path,
        dek: bytes | None,
        proxy: str | None = None,
    ) -> None:
        self._api_id = api_id
        self._api_hash = api_hash
        self._session_path = session_path
        self._dek = dek
        self._proxy = proxy
        self._client: TelegramClient | None = None
        self._string_session: StringSession | None = None
        self._pending_phone: str | None = None
        self._pending_phone_hash: str | None = None
        # Set by ``start_new_message_watcher`` so the watcher survives
        # client reconnects: every time ``_get_client`` builds a fresh
        # client we re-attach the NewMessage handler against this
        # factory. ``None`` keeps the manager passive (no DB writes).
        self._watcher_session_factory: Any | None = None

    # -------- session persistence --------

    def _load_string_session(self) -> StringSession:
        """Decrypt saved session string, or return empty one if not present."""
        if not self._session_path.exists():
            return StringSession()
        if self._dek is None:
            raise TelegramAuthError(
                "locked", "Cannot load Telegram session while the vault is locked"
            )
        blob = json.loads(self._session_path.read_bytes())
        pt = aead_decrypt(
            self._dek,
            base64.b64decode(blob["nonce"]),
            base64.b64decode(blob["ciphertext"]),
            base64.b64decode(blob["tag"]),
        )
        return StringSession(pt.decode("ascii"))

    def _save_string_session(self, s: StringSession) -> None:
        if self._dek is None:
            raise TelegramAuthError(
                "locked", "Cannot save Telegram session while the vault is locked"
            )
        raw = s.save().encode("ascii")
        out = aead_encrypt(self._dek, raw)
        self._session_path.parent.mkdir(parents=True, exist_ok=True)
        self._session_path.write_bytes(
            json.dumps({k: base64.b64encode(v).decode() for k, v in out.items()}).encode("utf-8")
        )

    # -------- client lifecycle --------

    async def _get_client(self) -> TelegramClient:
        # Reconnect a previously-built client that has since dropped its socket
        # (e.g. proxy hiccup, transient network drop, server restart).
        if self._client is not None:
            try:
                if self._client.is_connected():
                    return self._client
            except Exception:  # noqa: BLE001 — defensive
                pass
            try:
                log.info("telethon_client_reconnecting")
                await self._client.connect()
                if self._client.is_connected():
                    return self._client
            except Exception as exc:  # noqa: BLE001
                log.warning("telethon_client_reconnect_failed", error=str(exc))
            # Couldn't revive — drop and rebuild from scratch.
            with contextlib.suppress(Exception):
                await self._client.disconnect()
            self._client = None
            self._string_session = None
        s = self._load_string_session()
        self._string_session = s
        connection = None
        proxy_arg = None
        if self._proxy:
            from src.telegram.proxy import parse_proxy

            try:
                p = parse_proxy(self._proxy)
            except ValueError as exc:
                log.warning("telethon_proxy_parse_failed", proxy=self._proxy, error=str(exc))
                raise TelegramAuthError("proxy_invalid", str(exc)) from exc
            if p is not None:
                connection, proxy_arg = p.to_telethon()
                log.info(
                    "telethon_proxy_resolved",
                    kind=p.kind,
                    host=p.host,
                    port=p.port,
                    secret_bytes=(len(bytes.fromhex(p.secret_hex)) if p.secret_hex else None),
                    connection=connection.__name__ if connection else None,
                )
        else:
            log.info("telethon_no_proxy")

        kwargs: dict[str, Any] = {}
        if connection is not None:
            kwargs["connection"] = connection
        if proxy_arg is not None:
            kwargs["proxy"] = proxy_arg
        log.info("telethon_client_connecting", api_id=self._api_id)
        self._client = TelegramClient(s, self._api_id, self._api_hash, **kwargs)
        try:
            await self._client.connect()
        except Exception as exc:
            log.exception(
                "telethon_client_connect_failed",
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise
        log.info("telethon_client_connected")
        # Re-attach the NewMessage watcher onto the fresh client object so
        # a reconnect (or post-restart rebuild) doesn't silently drop the
        # real-time last_message_date updates.
        if self._watcher_session_factory is not None:
            try:
                self._register_new_message_handler(
                    self._client, self._watcher_session_factory
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "new_message_watcher_reattach_failed", error=str(exc)
                )
        return self._client

    def _register_new_message_handler(
        self, client: TelegramClient, session_factory: Any
    ) -> None:
        """Attach a NewMessage event handler on ``client`` that mirrors
        every fresh message's timestamp into ``chats.last_message_date``.

        Idempotent per-client: a handler attribute on the client object
        guards against double-registration. Errors inside the handler
        get swallowed at the DEBUG level — a transient DB lock or stale
        chat row must not crash Telethon's event loop.
        """
        if getattr(client, "_tge_new_message_handler", False):
            return
        from sqlalchemy import update as _update
        from telethon import events

        from src.db.models import Chat

        @client.on(events.NewMessage(incoming=True, outgoing=True))
        async def _on_new_message(event):  # noqa: ANN001 — telethon supplies type
            try:
                chat_id = int(event.chat_id)
            except (TypeError, ValueError):
                return
            msg_date = getattr(getattr(event, "message", None), "date", None)
            if msg_date is None:
                return
            try:
                async with session_factory() as s:
                    await s.execute(
                        _update(Chat)
                        .where(Chat.id == chat_id)
                        .values(last_message_date=msg_date)
                    )
                    await s.commit()
            except Exception as exc:  # noqa: BLE001
                log.debug(
                    "new_message_watcher_update_failed",
                    chat_id=chat_id,
                    error=str(exc),
                )

        client._tge_new_message_handler = True  # type: ignore[attr-defined]
        log.info("telethon_new_message_watcher_attached")

    async def start_new_message_watcher(self, session_factory: Any) -> None:
        """Public entry point — register the NewMessage handler and
        remember the session factory so reconnects re-attach it
        automatically.
        """
        self._watcher_session_factory = session_factory
        client = await self._get_client()
        self._register_new_message_handler(client, session_factory)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.disconnect()
            self._client = None

    async def is_authorized(self) -> bool:
        client = await self._get_client()
        return await client.is_user_authorized()

    # -------- auth flow --------

    async def start_login(self, phone: str) -> AuthStep:
        """Send SMS/app code. Returns the next step."""
        client = await self._get_client()
        try:
            sent = await client.send_code_request(phone)
        except PhoneNumberInvalidError as exc:
            raise TelegramAuthError("phone_invalid", str(exc)) from exc
        except FloodWaitError as exc:
            raise TelegramAuthError(
                "flood_wait", f"Wait {exc.seconds}s before retrying"
            ) from exc
        self._pending_phone = phone
        self._pending_phone_hash = sent.phone_code_hash
        return AuthStep.CODE

    async def submit_code(self, code: str) -> AuthStep:
        """Submit the SMS/app code. May escalate to 2FA password step."""
        client = await self._get_client()
        if self._pending_phone is None or self._pending_phone_hash is None:
            raise TelegramAuthError("state_error", "No pending login. Call start_login first.")
        try:
            await client.sign_in(
                phone=self._pending_phone,
                code=code,
                phone_code_hash=self._pending_phone_hash,
            )
        except PhoneCodeInvalidError as exc:
            raise TelegramAuthError("code_invalid", str(exc)) from exc
        except PhoneCodeExpiredError as exc:
            raise TelegramAuthError("code_expired", str(exc)) from exc
        except SessionPasswordNeededError:
            return AuthStep.PASSWORD
        assert self._string_session is not None
        self._save_string_session(self._string_session)
        return AuthStep.READY

    async def submit_password(self, password: str) -> AuthStep:
        """Finish the 2FA step."""
        client = await self._get_client()
        try:
            await client.sign_in(password=password)
        except PasswordHashInvalidError as exc:
            raise TelegramAuthError("password_invalid", str(exc)) from exc
        assert self._string_session is not None
        self._save_string_session(self._string_session)
        return AuthStep.READY

    # -------- dialog enumeration --------

    async def iter_dialogs(self) -> AsyncIterator[DialogEntry]:
        client = await self._get_client()
        # Resolve folders → {chat_id: [folder_ids]}.
        chat_folders = await self._chat_folder_index(client)

        async for d in client.iter_dialogs(limit=None, archived=None):
            entity = d.entity
            # Build the displayable name with proper fallback for User entities
            # (private chats / bots have first_name / last_name, not title).
            title = (
                getattr(d, "name", None)
                or getattr(entity, "title", None)
                or _readable_name(entity)
            )
            username = getattr(entity, "username", None)
            entity_type = _dialog_type(entity)
            # Approximate message count.
            #
            # Strategy depends on the dialog type:
            #
            #   * DMs / Bots (User entity): use ``get_messages(peer,
            #     limit=0).total`` — ``top_message.id`` is the global
            #     per-account counter and is useless (user reported
            #     "Андрей 462533 сообщений" when the real DM had 194).
            #
            #   * Groups / Supergroups: use the **max** of
            #     ``top_message.id`` and ``get_messages.total`` so we
            #     never undercount. ``get_messages.total`` is the
            #     ``messages.getHistory`` MessagesSlice.count which
            #     can quietly exclude service messages and posts made
            #     "от лица группы" (anonymous-admin / channel-as-group
            #     posts). ``top_message.id`` is the per-chat counter
            #     and includes everything ever sent. Basic groups
            #     rarely have heavy deletions, so the two values are
            #     close in practice; when they diverge we trust the
            #     larger value (user complaint: "сообщения в группе
            #     считаются неправильно — публикации от лица группы
            #     это тоже сообщение").
            #
            #   * Broadcast channels: stay with ``get_messages.total``.
            #     ``top_message.id`` lies because of large-scale
            #     deletions over the years (CATIZEN: id=2872704 vs
            #     real 696197 — using max() would overcount 4×).
            #
            # Cost: +1 API call per dialog at refresh time. Telethon
            # handles ``GetHistoryRequest flood wait`` internally with
            # its own ``flood_sleep_threshold`` — a 1k-chat refresh
            # takes ~10 minutes including the cumulative wait, which is
            # acceptable for an action the user invokes manually. Falls
            # back to None on error so a flaky peer doesn't break the
            # whole refresh.
            approx_count: int | None = None
            try:
                hits = await client.get_messages(entity, limit=0)
                total = getattr(hits, "total", None)
                if isinstance(total, int):
                    approx_count = total
            except Exception as exc:  # noqa: BLE001
                log.debug(
                    "dialog_total_lookup_failed",
                    chat_id=int(d.id),
                    error=str(exc),
                )

            # For basic groups + supergroups, top_message.id is the
            # per-chat id space counter; messages posted "от лица группы"
            # (anonymous admins, linked-channel forwards) and service
            # messages all consume an id, so the id is at least as
            # inclusive as any history search. Take whichever value is
            # higher so we don't undercount.
            if entity_type in (ChatType.GROUP, ChatType.SUPERGROUP):
                top_msg = getattr(d, "message", None)
                top_msg_id = getattr(top_msg, "id", None) if top_msg else None
                if (
                    isinstance(top_msg_id, int)
                    and top_msg_id > 0
                    and (approx_count is None or top_msg_id > approx_count)
                ):
                    approx_count = top_msg_id

            yield DialogEntry(
                id=int(d.id),
                title=str(title),
                username=username,
                type=entity_type,
                is_archived=bool(getattr(d, "archived", False)),
                approx_message_count=approx_count,
                last_message_date=getattr(d, "date", None),
                first_name=(getattr(entity, "first_name", None) or None),
                last_name=(getattr(entity, "last_name", None) or None),
                is_public=bool(username),
                folder_ids=chat_folders.get(int(d.id), []),
            )

    async def _chat_folder_index(self, client) -> dict[int, list[int]]:  # noqa: ANN001
        """Return {chat_id: [folder_id, ...]} from the user's Telegram folders.

        Folders are exposed by Telethon as DialogFilter objects via
        `messages.GetDialogFilters`. We only care about included peers; the
        special `archive` and `default` filters get folder_id=0/1 which we
        skip — the UI already handles 'archived' on its own.
        """
        try:
            from telethon.tl.functions.messages import (  # type: ignore[import-not-found]
                GetDialogFiltersRequest,
            )

            filters = await client(GetDialogFiltersRequest())
        except Exception as exc:  # noqa: BLE001
            log.warning("get_dialog_filters_failed", error=str(exc))
            return {}

        # Telethon may wrap response in `.filters` (newer) or return list.
        raw = getattr(filters, "filters", None) or filters or []
        index: dict[int, list[int]] = {}
        for f in raw:
            fid = getattr(f, "id", None)
            if fid is None:
                continue
            for peer in getattr(f, "include_peers", None) or []:
                pid = self._peer_to_chat_id(peer)
                if pid is None:
                    continue
                index.setdefault(pid, []).append(int(fid))
            for peer in getattr(f, "pinned_peers", None) or []:
                pid = self._peer_to_chat_id(peer)
                if pid is None:
                    continue
                lst = index.setdefault(pid, [])
                if int(fid) not in lst:
                    lst.append(int(fid))
        return index

    @staticmethod
    def _peer_to_chat_id(peer: Any) -> int | None:
        """Convert a Telethon InputPeer* into the same chat id format we
        store in the DB (negative for groups/channels)."""
        # InputPeerUser
        uid = getattr(peer, "user_id", None)
        if uid is not None:
            return int(uid)
        # InputPeerChat
        cid = getattr(peer, "chat_id", None)
        if cid is not None:
            return -int(cid)
        # InputPeerChannel — Telegram client format = -100<channel_id>
        ch = getattr(peer, "channel_id", None)
        if ch is not None:
            return int(f"-100{ch}")
        return None

    async def list_folders(self) -> list[DialogFolder]:
        """Return user-defined Telegram folders with their member chat ids."""
        client = await self._get_client()
        try:
            from telethon.tl.functions.messages import (  # type: ignore[import-not-found]
                GetDialogFiltersRequest,
            )

            filters = await client(GetDialogFiltersRequest())
        except Exception as exc:  # noqa: BLE001
            log.warning("list_folders_failed", error=str(exc))
            return []

        raw = getattr(filters, "filters", None) or filters or []
        out: list[DialogFolder] = []
        for f in raw:
            fid = getattr(f, "id", None)
            title = getattr(f, "title", None)
            if fid is None or not title:
                continue
            # `title` is sometimes a TextWithEntities — extract .text.
            title_text = getattr(title, "text", title)
            chat_ids: list[int] = []
            for peer in (
                list(getattr(f, "include_peers", None) or [])
                + list(getattr(f, "pinned_peers", None) or [])
            ):
                pid = self._peer_to_chat_id(peer)
                if pid is not None and pid not in chat_ids:
                    chat_ids.append(pid)
            out.append(DialogFolder(id=int(fid), title=str(title_text), chat_ids=chat_ids))
        return out

    async def download_avatar(self, entity_id: int, dest: Path) -> Path | None:
        """Download a 160px profile photo; returns path or None if missing."""
        client = await self._get_client()
        try:
            entity = await client.get_entity(entity_id)
        except Exception as exc:  # pragma: no cover - network-dependent
            log.warning("avatar_entity_lookup_failed", id=entity_id, error=str(exc))
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        path = await client.download_profile_photo(entity, file=str(dest))
        return Path(path) if path else None

    async def me_id(self) -> int | None:
        """Return the logged-in user's id, or None if not signed in."""
        client = await self._get_client()
        try:
            me = await client.get_me()
        except Exception as exc:  # noqa: BLE001
            log.warning("get_me_failed", error=str(exc))
            return None
        return getattr(me, "id", None)

    async def iter_message_senders(
        self, chat_id: int, ids: list[int] | None = None
    ) -> AsyncIterator[tuple[int, int | None]]:
        """Yield ``(message_id, sender_id)`` pairs for the chat.

        When ``ids`` is given, fetch exactly those messages (Telethon
        chunks the lookup automatically). When ``ids`` is None, walk
        the full history. Telethon raises ``FloodWaitError`` on rate
        limits — the caller is expected to ``asyncio.sleep`` and retry.
        """
        client = await self._get_client()
        if ids:
            # Telethon's `ids=` accepts up to 200 per call; chunking is
            # done internally when the list is longer.
            async for msg in client.iter_messages(chat_id, ids=ids):
                if msg is None:
                    continue
                yield msg.id, getattr(msg, "sender_id", None) or getattr(msg, "from_id", None)
            return
        async for msg in client.iter_messages(chat_id, limit=None):
            yield msg.id, getattr(msg, "sender_id", None) or getattr(msg, "from_id", None)
