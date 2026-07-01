"""Phase 1 smoke: FastAPI app boots, /health and / respond."""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.main import create_app


def test_health_ok() -> None:
    app = create_app()
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_index_html(tmp_settings) -> None:  # noqa: ARG001 — fixture monkey-patches env
    """With a freshly-created vault, hitting / should land on login page
    (middleware redirects there). Both landing pages embed the CDN wiring."""
    app = create_app()
    with TestClient(app) as client:
        r = client.get("/", follow_redirects=True)
    assert r.status_code == 200
    # Base-layout wiring is present regardless of which route served us:
    assert "cdn.tailwindcss.com" in r.text
    assert "htmx.org" in r.text
    assert "alpinejs" in r.text
    # Brand appears in the header, possibly as non-breaking spaces:
    assert "Telegram" in r.text
    assert "Exporter" in r.text


def test_static_mount_available_when_dir_exists() -> None:
    """Static mount should not error when src/web/static exists (may be empty)."""
    app = create_app()
    with TestClient(app) as client:
        # The static dir exists but is empty; 404 for non-existent file is fine.
        r = client.get("/static/missing.css")
    assert r.status_code in (404, 200)
