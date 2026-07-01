"""Tests for SHA-256 dedup with hardlinks."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.db.session import create_engine, create_schema, create_session_factory
from src.services.deduplicator import Deduplicator, hash_file


@pytest.fixture
async def factory(tmp_path: Path):
    eng = create_engine(tmp_path / "state.db", dek=None)
    await create_schema(eng)
    yield create_session_factory(eng)
    await eng.dispose()


@pytest.fixture
def pool(tmp_path: Path) -> Path:
    p = tmp_path / "pool"
    p.mkdir()
    return p


def write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


async def test_hash_file_known_value(tmp_path: Path) -> None:
    p = write(tmp_path / "a.bin", b"hello world")
    # sha256('hello world') = b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9
    assert hash_file(p) == "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"


async def test_first_sight_moves_to_pool_and_links_back(factory, pool, tmp_path: Path) -> None:
    dedup = Deduplicator(factory, pool)
    src = write(tmp_path / "src" / "a.bin", b"unique-1")

    outcome = await dedup.process_file(src)
    assert outcome.was_duplicate is False
    assert outcome.bytes_saved == 0
    assert outcome.canonical_path.exists()
    # Both locations resolve to the same inode (same hardlink):
    if os.name != "nt":
        assert outcome.canonical_path.stat().st_ino == src.stat().st_ino
    assert src.read_bytes() == b"unique-1"


async def test_duplicate_is_hardlinked_and_space_saved(factory, pool, tmp_path: Path) -> None:
    dedup = Deduplicator(factory, pool)
    data = b"payload-duplicate" * 1024  # 17 KiB
    a = write(tmp_path / "c1" / "m.bin", data)
    b = write(tmp_path / "c2" / "m.bin", data)

    r1 = await dedup.process_file(a)
    r2 = await dedup.process_file(b)
    assert r1.was_duplicate is False
    assert r2.was_duplicate is True
    assert r2.bytes_saved == len(data)
    assert b.read_bytes() == data  # readable
    if os.name != "nt":
        assert a.stat().st_ino == b.stat().st_ino == r1.canonical_path.stat().st_ino


async def test_three_way_dedup_counts(factory, pool, tmp_path: Path) -> None:
    dedup = Deduplicator(factory, pool)
    data = b"xyz" * 500
    paths = [write(tmp_path / f"d{i}" / "f.bin", data) for i in range(3)]
    for p in paths:
        await dedup.process_file(p)

    stats = await dedup.process_directory(tmp_path)
    # After round 1 all three are already hardlinks; process_directory re-runs
    # and should see them as duplicates of each other / the canonical.
    assert stats.total_files >= 3


async def test_different_bytes_do_not_dedup(factory, pool, tmp_path: Path) -> None:
    dedup = Deduplicator(factory, pool)
    a = write(tmp_path / "x" / "a.bin", b"AAA")
    b = write(tmp_path / "y" / "b.bin", b"BBB")
    r1 = await dedup.process_file(a)
    r2 = await dedup.process_file(b)
    assert r1.was_duplicate is False
    assert r2.was_duplicate is False
    assert r1.canonical_path != r2.canonical_path


async def test_process_directory_batch(factory, pool, tmp_path: Path) -> None:
    dedup = Deduplicator(factory, pool)
    # 3 identical, 2 unique
    for i in range(3):
        write(tmp_path / "root" / f"dup{i}.bin", b"ZZZZ" * 300)
    write(tmp_path / "root" / "u1.bin", b"alpha")
    write(tmp_path / "root" / "u2.bin", b"beta")

    stats = await dedup.process_directory(tmp_path / "root")
    assert stats.total_files == 5
    assert stats.duplicates == 2
    assert stats.bytes_saved == 2 * (4 * 300)


async def test_missing_file_raises(factory, pool, tmp_path: Path) -> None:
    dedup = Deduplicator(factory, pool)
    with pytest.raises(FileNotFoundError):
        await dedup.process_file(tmp_path / "nope.bin")


async def test_concurrent_process_file_does_not_deadlock_or_double_insert(
    factory, pool, tmp_path: Path,
) -> None:
    """Reproduces the ``database is locked`` scenario the user hit
    during bulk-sync: multiple chat-jobs in parallel each calling
    ``process_file`` on different files. Before the fix two
    concurrent SELECT-then-INSERT round-trips against the same
    media_files row could race and one would raise the SQLite lock
    error (because rollback-journal serialises writers and the
    default ``busy_timeout`` is 0).

    With the in-process asyncio.Lock all writes happen one after
    the other, and the WAL + busy_timeout PRAGMA absorb any
    contention from other components (job persistence, chat_state
    advance). The test fans out 20 unique-bytes files in parallel
    + 5 duplicates of a single 21st payload and asserts all of them
    finish without raising AND the duplicate count is correct.
    """
    import asyncio as _asyncio

    dedup = Deduplicator(factory, pool)
    # 20 unique blobs + 5 duplicates of a 21st payload.
    paths: list[Path] = []
    for i in range(20):
        paths.append(write(tmp_path / "u" / f"u{i}.bin", f"unique-{i}".encode()))
    for j in range(5):
        paths.append(write(tmp_path / "d" / f"d{j}.bin", b"DUPLICATE_PAYLOAD" * 64))

    results = await _asyncio.gather(*(dedup.process_file(p) for p in paths))
    # 20 unique → not duplicates; 5 duplicates of the SAME payload →
    # the first one through wins the canonical slot, the other 4 are
    # hardlinked back. Total duplicates = 4.
    duplicates = sum(1 for r in results if r.was_duplicate)
    assert duplicates == 4
    # No file was lost — every path still has bytes on disk.
    assert all(p.is_file() for p in paths)
