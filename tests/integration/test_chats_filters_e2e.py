"""End-to-end UI tests for /chats filters.

Bypass the session-gate middleware so we can hit /chats directly without
a real login flow, then exercise every query-param combination the UI
emits and assert the rendered grid contains the expected rows.

If you change the filter contract (add a pill, change a query param
shape, rename a CSS hook), update these tests — they're the canonical
proof that the UI keeps working.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.config import reset_settings_cache
from src.db.models import Chat, ChatType, DialogFolderRow
from src.db.session import create_engine, create_schema, create_session_factory


@pytest.fixture
def isolated_app(tmp_path: Path, monkeypatch):  # noqa: ANN001
    """Boot the app against a tmp DATA_DIR and pre-populate a small set
    of dialogs covering every type, recency bucket, and folder."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path / "exports"))
    monkeypatch.setenv("PROXY_AUTO_SELECT", "false")
    reset_settings_cache()

    # Seed the DB synchronously before the app starts.
    import asyncio
    import json

    async def seed():
        eng = create_engine(tmp_path / "data" / "state.db", dek=None)
        await create_schema(eng)
        f = create_session_factory(eng)
        now = datetime.now(UTC)
        async with f() as s:
            s.add(
                DialogFolderRow(id=1, title="Работа", chat_count=2)
            )
            s.add(Chat(
                id=10, title="Илья Шмаков", username="ilyshka1",
                type=ChatType.PRIVATE, is_public=True,
                first_name="Илья", last_name="Шмаков",
                last_message_date=now,
                folder_ids_json=json.dumps([1]),
            ))
            s.add(Chat(
                id=20, title="Старый канал", username="old_channel",
                type=ChatType.CHANNEL, is_public=True,
                last_message_date=now - timedelta(days=4 * 365),
            ))
            s.add(Chat(
                id=30, title="Группа без даты", type=ChatType.GROUP,
                last_message_date=None,
            ))
            s.add(Chat(
                id=40, title="Бот", username="some_bot",
                type=ChatType.BOT, is_public=True,
                last_message_date=now - timedelta(days=180),
                folder_ids_json=json.dumps([1]),
            ))
            s.add(Chat(
                id=50, title="Супергруппа Свежая", type=ChatType.SUPERGROUP,
                last_message_date=now - timedelta(days=10),
            ))
            await s.commit()
        await eng.dispose()

    asyncio.run(seed())

    from src.main import create_app

    monkeypatch.setattr("src.main._is_public_path", lambda _p: True)
    app = create_app()
    with TestClient(app) as client:
        yield client
    reset_settings_cache()


def _ids(html: str) -> list[int]:
    """Extract data-chat-id values from rendered HTML."""
    import re

    return [int(m) for m in re.findall(r'data-chat-id="(-?\d+)"', html)]


# ---------- Type filters ----------


def test_type_all(isolated_app: TestClient) -> None:
    r = isolated_app.get("/chats/fragment?type=all&recency=&folder=")
    assert r.status_code == 200
    assert set(_ids(r.text)) == {10, 20, 30, 40, 50}


def test_type_private(isolated_app: TestClient) -> None:
    r = isolated_app.get("/chats/fragment?type=private&recency=&folder=")
    assert r.status_code == 200
    assert _ids(r.text) == [10]


def test_type_bot(isolated_app: TestClient) -> None:
    r = isolated_app.get("/chats/fragment?type=bot&recency=&folder=")
    assert r.status_code == 200
    assert _ids(r.text) == [40]


def test_type_channel(isolated_app: TestClient) -> None:
    r = isolated_app.get("/chats/fragment?type=channel&recency=&folder=")
    assert r.status_code == 200
    assert _ids(r.text) == [20]


def test_type_group_supergroup(isolated_app: TestClient) -> None:
    assert _ids(isolated_app.get(
        "/chats/fragment?type=group&recency=&folder=").text) == [30]
    assert _ids(isolated_app.get(
        "/chats/fragment?type=supergroup&recency=&folder=").text) == [50]


def test_type_public(isolated_app: TestClient) -> None:
    r = isolated_app.get("/chats/fragment?type=public&recency=&folder=")
    assert set(_ids(r.text)) == {10, 20, 40}  # ones with is_public=True


# ---------- Recency filters ----------


def test_recency_blank_keeps_all(isolated_app: TestClient) -> None:
    r = isolated_app.get("/chats/fragment?type=all&recency=&folder=")
    assert r.status_code == 200
    assert set(_ids(r.text)) == {10, 20, 30, 40, 50}


def test_recency_one_year(isolated_app: TestClient) -> None:
    """Within last 1 year: ilyshka1 (now), bot (180d), supergroup (10d).
    Excluded: old_channel (4y), no-date group."""
    r = isolated_app.get("/chats/fragment?type=all&recency=1&folder=")
    assert r.status_code == 200
    assert set(_ids(r.text)) == {10, 40, 50}


def test_recency_three_years(isolated_app: TestClient) -> None:
    r = isolated_app.get("/chats/fragment?type=all&recency=3&folder=")
    assert r.status_code == 200
    assert set(_ids(r.text)) == {10, 40, 50}


def test_recency_handles_null_string(isolated_app: TestClient) -> None:
    """The UI sometimes posts the literal 'null' as recency value."""
    r = isolated_app.get("/chats/fragment?type=all&recency=null&folder=")
    assert r.status_code == 200
    assert set(_ids(r.text)) == {10, 20, 30, 40, 50}


# ---------- Folder filter ----------


def test_folder_filter(isolated_app: TestClient) -> None:
    r = isolated_app.get("/chats/fragment?type=all&recency=&folder=1")
    assert r.status_code == 200
    assert set(_ids(r.text)) == {10, 40}  # tagged with folder 1


def test_folder_unknown_returns_empty(isolated_app: TestClient) -> None:
    r = isolated_app.get("/chats/fragment?type=all&recency=&folder=9999")
    assert r.status_code == 200
    assert _ids(r.text) == []


# ---------- Search ----------


def test_search_by_first_name(isolated_app: TestClient) -> None:
    r = isolated_app.get(
        "/chats/fragment?type=all&recency=&folder=&q=" + "Илья"
    )
    assert _ids(r.text) == [10]


def test_search_by_last_name(isolated_app: TestClient) -> None:
    r = isolated_app.get(
        "/chats/fragment?type=all&recency=&folder=&q=" + "Шмаков"
    )
    assert _ids(r.text) == [10]


def test_search_by_username_partial(isolated_app: TestClient) -> None:
    r = isolated_app.get(
        "/chats/fragment?type=all&recency=&folder=&q=ilyshka"
    )
    assert _ids(r.text) == [10]


def test_search_by_title_partial(isolated_app: TestClient) -> None:
    r = isolated_app.get(
        "/chats/fragment?type=all&recency=&folder=&q=" + "Свежая"
    )
    assert _ids(r.text) == [50]


def test_search_no_match(isolated_app: TestClient) -> None:
    r = isolated_app.get(
        "/chats/fragment?type=all&recency=&folder=&q=" + "несуществующий"
    )
    assert r.status_code == 200
    assert _ids(r.text) == []


# ---------- Combined filters ----------


def test_combined_type_and_recency(isolated_app: TestClient) -> None:
    """Bots active in last year."""
    r = isolated_app.get("/chats/fragment?type=bot&recency=1&folder=")
    assert _ids(r.text) == [40]


def test_combined_folder_and_search(isolated_app: TestClient) -> None:
    r = isolated_app.get(
        "/chats/fragment?type=all&recency=&folder=1&q=" + "Илья"
    )
    assert _ids(r.text) == [10]


def test_combined_no_match(isolated_app: TestClient) -> None:
    r = isolated_app.get(
        "/chats/fragment?type=channel&recency=1&folder=&q=" + "Илья"
    )
    assert _ids(r.text) == []


# ---------- Counter (12.11) ----------


def test_fragment_includes_counter_oob(isolated_app: TestClient) -> None:
    """The fragment carries an OOB-swap counter element with sane numbers."""
    r = isolated_app.get("/chats/fragment?type=all&recency=&folder=")
    assert r.status_code == 200
    # OOB span present, with both filtered and total counts.
    assert 'id="chats-counter"' in r.text
    assert 'hx-swap-oob="true"' in r.text
    # All five rows match an unfiltered query → "5 из 5 в списке".
    assert "5 из 5 в списке" in r.text


def test_fragment_counter_shrinks_when_filtered(isolated_app: TestClient) -> None:
    """A narrow query reduces filtered_count but keeps total_count."""
    r = isolated_app.get(
        "/chats/fragment?type=all&recency=&folder=&q=" + "Илья"
    )
    assert r.status_code == 200
    assert 'id="chats-counter"' in r.text
    # 1 of 5 rows match the query.
    assert "1 из 5 в списке" in r.text


def test_fragment_q_foo_returns_counter(isolated_app: TestClient) -> None:
    """`q=foo` matches none of the seed chats → '0 из 5'."""
    r = isolated_app.get("/chats/fragment?q=foo")
    assert r.status_code == 200
    assert 'id="chats-counter"' in r.text
    # filtered_count <= total_count must hold.
    import re

    m = re.search(r"(\d+)\s*из\s*(\d+)\s*в списке", r.text)
    assert m is not None, "counter element not found"
    filtered, total = int(m.group(1)), int(m.group(2))
    assert filtered <= total
    assert total == 5
    assert filtered == 0


# ---------- Refresh-stamp marker (13.3) ----------


def test_refresh_stamp_round_trip(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """write_refresh_stamp writes an ISO string; read_refresh_stamp parses it."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path / "exports"))
    reset_settings_cache()
    from src.api._chats_refresh_stamp import (
        humanise_refreshed,
        read_refresh_stamp,
        refresh_stamp_path,
        write_refresh_stamp,
    )

    # Initially missing.
    assert read_refresh_stamp() is None

    iso = write_refresh_stamp()
    assert isinstance(iso, str) and len(iso) > 0
    assert refresh_stamp_path().exists()

    dt = read_refresh_stamp()
    assert dt is not None
    # Same ISO string round-trips.
    assert dt.isoformat() == iso

    # Humanised label is non-empty for a fresh stamp.
    label = humanise_refreshed(dt)
    assert label is not None and len(label) > 0
    reset_settings_cache()


def test_humanise_refreshed_buckets() -> None:
    """Spot-check the relative-time vocabulary covers minutes/hours/days."""
    from src.api._chats_refresh_stamp import humanise_refreshed

    now = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)
    # 30 seconds ago.
    assert humanise_refreshed(
        now - timedelta(seconds=30), now=now
    ) == "только что"
    # 5 minutes ago.
    assert humanise_refreshed(
        now - timedelta(minutes=5), now=now
    ) == "5 мин назад"
    # 3 hours ago.
    assert humanise_refreshed(
        now - timedelta(hours=3), now=now
    ) == "3 ч назад"
    # Yesterday.
    assert humanise_refreshed(
        now - timedelta(days=1), now=now
    ) == "вчера"
    # 4 days ago.
    assert humanise_refreshed(
        now - timedelta(days=4), now=now
    ) == "4 дн назад"
    # >7 days falls back to ISO date.
    assert humanise_refreshed(
        now - timedelta(days=30), now=now
    ).startswith("2026-")
    # None passthrough.
    assert humanise_refreshed(None) is None


def test_chats_page_reads_refresh_stamp(isolated_app: TestClient,
                                         tmp_path: Path) -> None:
    """Writing the marker file before /chats GET surfaces in the rendered HTML.

    We can't trigger a real /chats/refresh in this fixture (no Telegram
    manager), but we can drop the file directly and verify list_chats()
    picks it up — that's what the route does on cold load.
    """
    from src.api._chats_refresh_stamp import refresh_stamp_path

    stamp = refresh_stamp_path()
    stamp.parent.mkdir(parents=True, exist_ok=True)
    # Write a marker exactly 5 minutes in the past so we get a stable label.
    five_mins_ago = (
        datetime.now(UTC) - timedelta(minutes=5)
    ).isoformat()
    stamp.write_text(five_mins_ago, encoding="utf-8")

    r = isolated_app.get("/chats")
    assert r.status_code == 200
    # Either "5 мин назад" or "4 мин назад" depending on rounding — accept both.
    assert "мин назад" in r.text
    # The chats-counter element renders "filtered из total в списке".
    assert 'id="chats-counter"' in r.text
    assert "в списке" in r.text


def test_chats_page_handles_missing_stamp(isolated_app: TestClient) -> None:
    """Cold load with no marker still renders cleanly."""
    from src.api._chats_refresh_stamp import refresh_stamp_path

    p = refresh_stamp_path()
    if p.exists():
        p.unlink()
    r = isolated_app.get("/chats")
    assert r.status_code == 200
    # Counter still present (initial render).
    assert 'id="chats-counter"' in r.text
    # No "обновлён" phrase when the stamp is absent.
    # (We check the element is empty rather than absent — it's still in DOM.)
    assert "Список обновлён" not in r.text
