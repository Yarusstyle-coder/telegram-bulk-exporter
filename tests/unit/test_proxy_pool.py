"""ProxyPool round-trip + TCP-handshake measurement against an in-process server."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.services.proxy_pool import ProxyPool


@pytest.fixture
async def echo_server():
    """A short-lived TCP server that accepts any connection on a free port."""
    async def handle(reader, writer):  # noqa: ARG001
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass

    server = await asyncio.start_server(handle, host="127.0.0.1", port=0)
    sockname = server.sockets[0].getsockname()
    host, port = sockname[0], sockname[1]
    try:
        yield host, port
    finally:
        server.close()
        await server.wait_closed()


async def test_load_seeds_from_env(tmp_path: Path) -> None:
    pool = ProxyPool(
        tmp_path / "pool.json",
        env_seed=["socks5://1.2.3.4:1080", "mtproto://h:443?secret=" + "ee" * 16],
    )
    await pool.load()
    urls = [e.url for e in pool.list_entries()]
    assert urls == [
        "socks5://1.2.3.4:1080",
        "mtproto://h:443?secret=" + "ee" * 16,
    ]
    assert all(e.from_env for e in pool.list_entries())


async def test_invalid_env_entries_dropped(tmp_path: Path) -> None:
    pool = ProxyPool(tmp_path / "pool.json", env_seed=["zzz://nope"])
    await pool.load()
    assert pool.list_entries() == []


async def test_add_remove_round_trip(tmp_path: Path) -> None:
    pool = ProxyPool(tmp_path / "pool.json")
    await pool.load()
    e = await pool.add("socks5://h:1080", label="test")
    assert e.url == "socks5://h:1080"
    assert e.label == "test"
    # Re-load from disk and confirm persistence
    pool2 = ProxyPool(tmp_path / "pool.json")
    await pool2.load()
    assert [e.url for e in pool2.list_entries()] == ["socks5://h:1080"]
    # Remove
    assert await pool2.remove("socks5://h:1080") is True
    assert pool2.list_entries() == []


async def test_add_rejects_bad_url(tmp_path: Path) -> None:
    pool = ProxyPool(tmp_path / "pool.json")
    await pool.load()
    with pytest.raises(ValueError):
        await pool.add("not-a-url")


async def test_measure_picks_best_real_server(tmp_path: Path, echo_server) -> None:
    host, port = echo_server
    pool = ProxyPool(
        tmp_path / "pool.json",
        env_seed=[
            f"socks5://{host}:{port}",
            "socks5://127.0.0.1:1",  # very likely to fail / be rejected fast
        ],
    )
    await pool.load()
    winner = await pool.measure_and_pick_best(timeout=2.0)
    assert winner is not None
    assert winner.url == f"socks5://{host}:{port}"
    assert winner.last_status == "ok"
    assert winner.last_latency_ms is not None
    # Active URL is persisted
    assert pool.active_url() == winner.url


async def test_measure_marks_dead_host_as_fail_or_timeout(tmp_path: Path) -> None:
    pool = ProxyPool(
        tmp_path / "pool.json",
        env_seed=["socks5://127.0.0.1:1"],  # nothing listens
    )
    await pool.load()
    entries = await pool.measure_all(timeout=1.0)
    assert entries[0].last_status in ("fail", "timeout")
    assert entries[0].last_latency_ms is None


async def test_set_active_persists(tmp_path: Path) -> None:
    pool = ProxyPool(tmp_path / "pool.json")
    await pool.load()
    await pool.add("socks5://a:1")
    await pool.add("socks5://b:2")
    await pool.set_active("socks5://b:2")
    pool2 = ProxyPool(tmp_path / "pool.json")
    await pool2.load()
    assert pool2.active_url() == "socks5://b:2"


async def test_auto_pick_skips_mtproto_even_when_fast(tmp_path: Path, echo_server) -> None:
    """mtproto:// scheme is incompatible with Telethon (fake-TLS handshake);
    even if the server pings fast, auto-pick must NOT activate it."""
    host, port = echo_server
    pool = ProxyPool(
        tmp_path / "p.json",
        env_seed=[
            f"mtproto://{host}:{port}?secret=" + "ee" * 16,  # fast TCP, but Telethon-incompatible
            f"socks5://{host}:{port}",  # equally fast, but compatible
        ],
    )
    await pool.load()
    winner = await pool.measure_and_pick_best(timeout=2.0)
    assert winner is not None
    assert winner.url.startswith("socks5://")
    assert pool.active_url() == winner.url


async def test_auto_pick_returns_none_when_only_mtproto(tmp_path: Path, echo_server) -> None:
    host, port = echo_server
    pool = ProxyPool(
        tmp_path / "p.json",
        env_seed=[f"mtproto://{host}:{port}?secret=" + "ee" * 16],
    )
    await pool.load()
    winner = await pool.measure_and_pick_best(timeout=2.0)
    assert winner is None
    assert pool.active_url() is None  # cleared so Telethon goes direct


async def test_remove_clears_active_when_matching(tmp_path: Path) -> None:
    pool = ProxyPool(tmp_path / "pool.json")
    await pool.load()
    await pool.add("socks5://a:1")
    await pool.set_active("socks5://a:1")
    await pool.remove("socks5://a:1")
    assert pool.active_url() is None
