"""Periodic re-test ticker behaves like a long-running asyncio task."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.services.proxy_pool import ProxyPool


@pytest.fixture
async def echo_server():
    async def handle(reader, writer):  # noqa: ARG001
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass

    server = await asyncio.start_server(handle, host="127.0.0.1", port=0)
    sock = server.sockets[0].getsockname()
    try:
        yield sock[0], sock[1]
    finally:
        server.close()
        await server.wait_closed()


async def test_dead_entries_are_kept_after_measure(tmp_path: Path) -> None:
    """A timing-out entry stays in the pool with last_status='timeout'/'fail'."""
    pool = ProxyPool(
        tmp_path / "p.json",
        env_seed=["socks5://127.0.0.1:1"],
    )
    await pool.load()
    await pool.measure_all(timeout=0.5)
    assert len(pool.list_entries()) == 1
    e = pool.list_entries()[0]
    assert e.last_status in ("fail", "timeout")
    # Re-measure: still present, status may change but URL is preserved
    await pool.measure_all(timeout=0.5)
    assert pool.list_entries()[0].url == "socks5://127.0.0.1:1"


async def test_keep_current_if_ok(tmp_path: Path, echo_server) -> None:
    host, port = echo_server
    pool = ProxyPool(
        tmp_path / "p.json",
        env_seed=[
            f"socks5://{host}:{port}",
            f"socks5h://{host}:{port}",
        ],
    )
    await pool.load()
    await pool.set_active(f"socks5h://{host}:{port}")
    # With keep_current_if_ok the active stays even if the other one is faster.
    await pool.measure_and_pick_best(timeout=2.0, keep_current_if_ok=True)
    assert pool.active_url() == f"socks5h://{host}:{port}"


async def test_periodic_retest_cancels_cleanly(tmp_path: Path, echo_server) -> None:
    host, port = echo_server
    pool = ProxyPool(tmp_path / "p.json", env_seed=[f"socks5://{host}:{port}"])
    await pool.load()
    task = asyncio.create_task(
        pool.run_periodic_retest(interval_seconds=1, timeout=1.0),
        name="proxy-retest",
    )
    await asyncio.sleep(0.1)  # task is alive in its sleep loop
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_periodic_retest_no_op_when_pool_empty(tmp_path: Path) -> None:
    pool = ProxyPool(tmp_path / "p.json")
    await pool.load()
    # Should return immediately without spinning on sleep.
    await asyncio.wait_for(
        pool.run_periodic_retest(interval_seconds=10),
        timeout=1.0,
    )
