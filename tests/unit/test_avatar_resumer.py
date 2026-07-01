"""Tests for :class:`src.services.avatar_resumer.AvatarResumer`.

The resumer reconciles ``data/avatars/`` against the chats table on startup
and re-downloads anything that's missing, stale, or corrupt. We exercise each
classification branch with a fake ``telegram_manager`` (AsyncMock) so no
network is involved.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.db.models import Chat, ChatType
from src.db.session import create_engine, create_schema, create_session_factory
from src.services.avatar_resumer import AvatarResumer, ResumeStats

# A header-valid JPEG body padded past the 64-byte sanity threshold.
_VALID_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 80 + b"\xff\xd9"


async def _make_factory(tmp_path: Path, chat_ids: Iterable[int]):
    """Build a session factory seeded with placeholder Chat rows.

    Returned tuple is ``(factory, engine)`` — caller is responsible for
    disposing the engine. We hand back the engine because aiosqlite holds a
    file lock until ``dispose()``; on Windows that would otherwise prevent
    ``tmp_path`` cleanup.
    """
    eng = create_engine(tmp_path / "state.db", dek=None)
    await create_schema(eng)
    factory = create_session_factory(eng)
    async with factory() as s:
        for cid in chat_ids:
            s.add(Chat(id=cid, title=f"chat-{cid}", type=ChatType.PRIVATE))
        await s.commit()
    return factory, eng


@pytest.fixture
def avatars_dir(tmp_path: Path) -> Path:
    d = tmp_path / "avatars"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _set_mtime_old(path: Path, age_seconds: float) -> None:
    """Backdate a file's mtime so the resumer thinks it's stale."""
    past = time.time() - age_seconds
    os.utime(path, (past, past))


async def test_missing_avatar_is_redownloaded(
    tmp_path: Path, avatars_dir: Path
) -> None:
    """No file on disk → resumer should call download_avatar and write bytes."""
    factory, eng = await _make_factory(tmp_path, [1001])
    try:

        async def fake_download(chat_id: int, dest: Path) -> Path:
            dest.write_bytes(_VALID_JPEG)
            return dest

        tg = AsyncMock()
        tg.download_avatar.side_effect = fake_download

        resumer = AvatarResumer(factory, avatars_dir)
        stats = await resumer.scan_and_resume(tg)

        assert stats == ResumeStats(
            total_chats=1,
            valid=0,
            redownloaded=1,
            missing_after=0,
            placeholder_kept=0,
        )
        assert (avatars_dir / "1001.jpg").read_bytes() == _VALID_JPEG
        tg.download_avatar.assert_awaited_once()
    finally:
        await eng.dispose()


async def test_fresh_placeholder_is_kept(
    tmp_path: Path, avatars_dir: Path
) -> None:
    """A 0-byte file younger than the TTL is left alone — Telegram had no photo."""
    factory, eng = await _make_factory(tmp_path, [1002])
    try:
        placeholder = avatars_dir / "1002.jpg"
        placeholder.write_bytes(b"")

        tg = AsyncMock()

        resumer = AvatarResumer(
            factory, avatars_dir, placeholder_max_age_seconds=86400
        )
        stats = await resumer.scan_and_resume(tg)

        assert stats == ResumeStats(
            total_chats=1,
            valid=0,
            redownloaded=0,
            missing_after=0,
            placeholder_kept=1,
        )
        assert placeholder.exists()
        assert placeholder.stat().st_size == 0
        tg.download_avatar.assert_not_awaited()
    finally:
        await eng.dispose()


async def test_stale_placeholder_is_redownloaded(
    tmp_path: Path, avatars_dir: Path
) -> None:
    """A 0-byte file older than the TTL gets another shot at Telegram."""
    factory, eng = await _make_factory(tmp_path, [1003])
    try:
        placeholder = avatars_dir / "1003.jpg"
        placeholder.write_bytes(b"")
        # 8 days old — exceeds the 7-day default TTL.
        _set_mtime_old(placeholder, 8 * 86400)

        async def fake_download(chat_id: int, dest: Path) -> Path:
            dest.write_bytes(_VALID_JPEG)
            return dest

        tg = AsyncMock()
        tg.download_avatar.side_effect = fake_download

        resumer = AvatarResumer(factory, avatars_dir)
        stats = await resumer.scan_and_resume(tg)

        assert stats.redownloaded == 1
        assert stats.placeholder_kept == 0
        assert placeholder.read_bytes() == _VALID_JPEG
    finally:
        await eng.dispose()


async def test_corrupt_file_is_quarantined_and_redownloaded(
    tmp_path: Path, avatars_dir: Path
) -> None:
    """A non-empty file that doesn't start with FFD8FF is renamed to .bad and retried."""
    factory, eng = await _make_factory(tmp_path, [1004])
    try:
        bad = avatars_dir / "1004.jpg"
        bad.write_bytes(b"not a jpeg")

        async def fake_download(chat_id: int, dest: Path) -> Path:
            dest.write_bytes(_VALID_JPEG)
            return dest

        tg = AsyncMock()
        tg.download_avatar.side_effect = fake_download

        resumer = AvatarResumer(factory, avatars_dir)
        stats = await resumer.scan_and_resume(tg)

        assert stats.redownloaded == 1
        assert (avatars_dir / "1004.jpg.bad").read_bytes() == b"not a jpeg"
        assert bad.read_bytes() == _VALID_JPEG
    finally:
        await eng.dispose()


async def test_valid_jpeg_is_skipped(
    tmp_path: Path, avatars_dir: Path
) -> None:
    """A healthy JPEG is left alone — no download attempted."""
    factory, eng = await _make_factory(tmp_path, [1005])
    try:
        good = avatars_dir / "1005.jpg"
        good.write_bytes(_VALID_JPEG)

        tg = AsyncMock()

        resumer = AvatarResumer(factory, avatars_dir)
        stats = await resumer.scan_and_resume(tg)

        assert stats == ResumeStats(
            total_chats=1,
            valid=1,
            redownloaded=0,
            missing_after=0,
            placeholder_kept=0,
        )
        assert good.read_bytes() == _VALID_JPEG
        tg.download_avatar.assert_not_awaited()
    finally:
        await eng.dispose()


async def test_download_exception_does_not_poison_loop(
    tmp_path: Path, avatars_dir: Path
) -> None:
    """If one chat raises, others still get processed and the failure is counted."""
    factory, eng = await _make_factory(tmp_path, [1001, 1002, 1003, 1004, 1005])
    try:
        call_log: list[int] = []

        async def fake_download(chat_id: int, dest: Path) -> Path | None:
            call_log.append(chat_id)
            if chat_id == 1003:
                raise RuntimeError("boom")
            if chat_id == 1004:
                # Telegram says "no photo" — should land as fresh placeholder.
                return None
            dest.write_bytes(_VALID_JPEG)
            return dest

        tg = AsyncMock()
        tg.download_avatar.side_effect = fake_download

        resumer = AvatarResumer(factory, avatars_dir)
        stats = await resumer.scan_and_resume(tg)

        assert stats.total_chats == 5
        # 1001, 1002, 1004, 1005 land successfully (1004 as placeholder).
        # 1003 raised → counted under missing_after.
        assert stats.redownloaded == 4
        assert stats.missing_after == 1
        assert sorted(call_log) == [1001, 1002, 1003, 1004, 1005]
        # 1004 came back None → 0-byte placeholder written.
        assert (avatars_dir / "1004.jpg").exists()
        assert (avatars_dir / "1004.jpg").stat().st_size == 0
        # 1003 raised → no file landed.
        assert not (avatars_dir / "1003.jpg").exists()
    finally:
        await eng.dispose()


async def test_no_chats_is_a_no_op(tmp_path: Path) -> None:
    """Empty DB → empty stats, no download calls; avatars_dir is created."""
    factory, eng = await _make_factory(tmp_path, [])
    try:
        avatars = tmp_path / "avatars"
        tg = AsyncMock()
        resumer = AvatarResumer(factory, avatars)
        stats = await resumer.scan_and_resume(tg)
        assert stats == ResumeStats(
            total_chats=0,
            valid=0,
            redownloaded=0,
            missing_after=0,
            placeholder_kept=0,
        )
        tg.download_avatar.assert_not_awaited()
        # Resumer should have ensured the dir exists.
        assert avatars.exists()
    finally:
        await eng.dispose()
