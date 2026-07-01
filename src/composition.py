"""Composition root — wires concrete instances into `app.state`.

Called by the lifespan in `src/main.py`. Runs in two stages:

1. Without a DEK — DB is unencrypted, but we still want the proxy pool, tdl
   wrapper and (empty) job manager available to the setup/login pages.
2. After login — re-runs with the user's DEK so the DB is SQLCipher-backed.

Idempotent: calling `attach_runtime` again replaces the previous engine and
job manager cleanly, preserving the same proxy pool when it already exists.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from src.config import get_settings
from src.db.session import create_engine, create_schema, create_session_factory
from src.jobs.exporter import ExportRunner
from src.jobs.job_manager import JobManager
from src.jobs.persistence import JobPersistence
from src.logging_setup import get_logger
from src.services.auto_update import DEFAULT_TICK_SECONDS, AutoUpdateScheduler
from src.services.deduplicator import Deduplicator
from src.services.proxy_pool import ProxyPool
from src.services.tdl_wrapper import TdlWrapper
from src.telegram.proxy import parse_proxy

if TYPE_CHECKING:
    from fastapi import FastAPI

log = get_logger(__name__)


async def attach_runtime(app: FastAPI, *, dek: bytes | None = None) -> None:
    settings = get_settings()
    settings.ensure_dirs()

    # Proxy pool — kept across re-attachments so user-added entries don't vanish.
    import asyncio

    pool: ProxyPool | None = getattr(app.state, "proxy_pool", None)
    if pool is None:
        pool = ProxyPool(settings.proxy_pool_path, env_seed=settings.env_proxy_seed())
        await pool.load()
        app.state.proxy_pool = pool
        if settings.proxy_auto_select and pool.list_entries():
            try:
                await pool.measure_and_pick_best()
            except Exception as exc:  # noqa: BLE001
                log.warning("proxy_auto_select_failed", error=str(exc))

        # Telethon's MTProxy classes don't support fake-TLS handshake, which
        # is what most public MTProxy servers serve nowadays. If the active
        # entry is mtproto:// we deactivate it pre-emptively — direct
        # connection works whenever VPN is on, and the user can re-pick
        # explicitly via /proxy.
        active = pool.active_url()
        if active and active.startswith(("mtproto://", "mtproxy://", "tg://", "https://t.me/proxy", "http://t.me/proxy")):
            log.info("proxy_active_disabled_fake_tls_risk", was=active)
            await pool.set_active(None)

        # Background re-test loop — survives until detach. Switches the active
        # proxy automatically when the network situation changes (e.g. user
        # toggles VPN region and a previously-dead entry comes back).
        if (
            settings.proxy_auto_select
            and settings.proxy_test_interval_seconds > 0
            and pool.list_entries()
        ):
            task = asyncio.create_task(
                pool.run_periodic_retest(
                    interval_seconds=settings.proxy_test_interval_seconds,
                ),
                name="proxy-pool-retest",
            )
            app.state.proxy_pool_retest_task = task

    engine = create_engine(settings.db_path, dek=dek)
    await create_schema(engine)
    factory = create_session_factory(engine)
    app.state.db_engine = engine
    app.state.session_factory = factory

    # Active proxy → tdl. Only socks5/http translates; MTProto is dropped
    # (with a structured-log warning) so tdl doesn't fail-fast on startup.
    active_url = pool.active_url()
    tdl_proxy = _proxy_for_tdl(active_url)
    if active_url and tdl_proxy is None:
        log.info(
            "tdl_proxy_skipped_mtproto",
            active=active_url,
            note="tdl supports socks5/http only; install mtg bridge for MTProto",
        )

    # Sweep stale tdl.exe processes that may still be locking our
    # bolt-DB from a previous server crash or an asyncio cancellation
    # that didn't actually kill the child. Without this the first
    # sync after a hard restart fails with "Current database is used
    # by another process" until the user goes process-hunting.
    try:
        from src.services.tdl_orphan_killer import kill_orphan_tdl

        storage_root = settings.data_dir / "tdl"
        kill_orphan_tdl(storage_root)
    except Exception as exc:  # noqa: BLE001 — non-fatal, log and proceed
        log.warning("tdl_orphan_sweep_skipped", error=str(exc))

    tdl = TdlWrapper(
        settings=settings,
        binary_path=Path(settings.tdl_binary_path),
        namespace=settings.tdl_namespace,
        proxy=tdl_proxy,
    )
    app.state.tdl = tdl

    dedup = Deduplicator(factory, settings.media_pool_dir)
    app.state.deduplicator = dedup

    runner = ExportRunner(
        session_factory=factory,
        tdl_wrapper=tdl,
        deduplicator=dedup,
        export_dir=settings.export_dir,
        avatars_dir=settings.avatars_dir,
        telegram_manager_provider=lambda: getattr(app.state, "telegram_manager", None),
    )
    persistence = JobPersistence(factory)
    mgr = JobManager(runner=runner, persistence=persistence)
    restored = await mgr.restore_from_persistence()
    app.state.job_manager = mgr

    # Per-chat auto-update ticker. Polls watched chats and enqueues
    # incremental ``only_new`` sync jobs when a chat falls behind its last
    # export. tdl-login gating is left as None for v1 — the scheduler still
    # honours the per-chat ``watch_enabled`` opt-in, the staleness rule, and
    # the active-job dedup, and a submitted job that finds tdl logged out
    # simply fails its own run (visible in /jobs) rather than silently
    # never running. Wiring a real tdl-session probe is a follow-up.
    scheduler = AutoUpdateScheduler(
        session_factory=factory,
        job_manager=mgr,
        tdl_login_check=None,
    )
    app.state.auto_update_scheduler = scheduler
    app.state.auto_update_task = asyncio.create_task(
        scheduler.run_periodic(tick_seconds=DEFAULT_TICK_SECONDS),
        name="auto-update",
    )

    log.info(
        "runtime_attached",
        dek_present=dek is not None,
        active_proxy=active_url,
        jobs_restored=restored,
    )


async def detach_runtime(app: FastAPI) -> None:
    mgr = getattr(app.state, "job_manager", None)
    if mgr is not None:
        pending = [
            job.task
            for job in mgr.list_jobs()
            if job.task is not None and not job.task.done()
        ]
        for task in pending:
            task.cancel()
        # Drain the cancelled job tasks BEFORE disposing the engine below.
        # Each ``_safe_run`` owns an async DB session (status + persist
        # writes); if we dispose the engine while a job task is still
        # mid-write, an aiosqlite connection survives into the event
        # loop's shutdown and deadlocks it — on Windows the portal thread
        # hangs forever in ``GetQueuedCompletionStatus`` (TestClient
        # teardown never returns). Awaiting here lets each
        # task settle and release its connection back to the pool first.
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    # Stop the auto-update ticker. It owns a DB session inside ``_tick``, so
    # like the job tasks above it MUST be cancelled AND fully awaited BEFORE
    # ``engine.dispose()`` below — a task surviving dispose while holding an
    # aiosqlite connection deadlocks the loop on Windows (same failure class
    # as the job-task hang above). Awaiting here lets the cancelled tick
    # release its session back to the pool first.
    auto_task = getattr(app.state, "auto_update_task", None)
    if auto_task is not None and not auto_task.done():
        auto_task.cancel()
        try:
            await auto_task
        except (Exception, BaseException):  # noqa: BLE001
            pass
    for attr in ("auto_update_task", "auto_update_scheduler"):
        if hasattr(app.state, attr):
            delattr(app.state, attr)

    # Stop the proxy re-test ticker if running. The pool itself survives.
    retest_task = getattr(app.state, "proxy_pool_retest_task", None)
    if retest_task is not None and not retest_task.done():
        retest_task.cancel()
        try:
            await retest_task
        except (Exception, BaseException):  # noqa: BLE001
            pass
    if hasattr(app.state, "proxy_pool_retest_task"):
        delattr(app.state, "proxy_pool_retest_task")

    engine = getattr(app.state, "db_engine", None)
    if engine is not None:
        await engine.dispose()
    # NB: proxy_pool intentionally survives detach so user-added entries persist
    # across re-login; only DB-bound state is torn down.
    for attr in (
        "db_engine",
        "session_factory",
        "tdl",
        "deduplicator",
        "job_manager",
        "telegram_manager",
    ):
        if hasattr(app.state, attr):
            delattr(app.state, attr)


def _proxy_for_tdl(url: str | None) -> str | None:
    """Translate the active proxy URL to a tdl-friendly form, or None."""
    if not url:
        return None
    try:
        parsed = parse_proxy(url)
    except ValueError:
        return None
    if parsed is None:
        return None
    return parsed.to_tdl_url()
