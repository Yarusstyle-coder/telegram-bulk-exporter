"""Unit tests for ``_normalize_for_search`` — the ё/е folding that
makes the chat-list search forgiving of the common Russian
diaeresis-drop convention.

Live bug that motivated this: user typed "Тёмная" looking for a
channel titled "Темная комната" (no ё) and got nothing back even
though the channel was right there in the DB.
"""

from __future__ import annotations

from src.api.routes_chats import _normalize_for_search


def test_lowercases_input() -> None:
    assert _normalize_for_search("Hello") == "hello"
    assert _normalize_for_search("ВКЛАДКА") == "вкладка"


def test_yo_folded_to_ye() -> None:
    # Lower-case ё → е
    assert _normalize_for_search("тёмная комната") == "темная комната"
    # Upper-case Ё → е (we lower first, then translate; capital Ё
    # also has its own translate entry as defence-in-depth).
    assert _normalize_for_search("Тёмная Комната") == "темная комната"


def test_idempotent_when_no_yo() -> None:
    # Strings without ё are unchanged (after lower-case).
    assert _normalize_for_search("темная комната") == "темная комната"
    assert _normalize_for_search("hello world") == "hello world"


def test_yo_substring_match_via_normalize() -> None:
    # The actual production usage: needle and haystack both go through
    # the same normaliser, so either form of "ё/е" matches the other.
    needle = _normalize_for_search("Тёмная")
    hay_with_yo = _normalize_for_search("Тёмная комната")
    hay_without_yo = _normalize_for_search("Темная комната")
    assert needle in hay_with_yo
    assert needle in hay_without_yo


def test_ye_search_matches_yo_title() -> None:
    # Other direction: user types without ё, title has ё.
    needle = _normalize_for_search("Темная")
    hay = _normalize_for_search("Тёмная комната")
    assert needle in hay


def test_preserves_non_russian_chars() -> None:
    # Latin / digits / punctuation pass through.
    assert _normalize_for_search("Иванов (@ivan_ivanov)") == "иванов (@ivan_ivanov)"
    assert _normalize_for_search("Канал #1") == "канал #1"
