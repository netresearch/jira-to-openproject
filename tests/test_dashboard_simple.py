# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Smoke tests for the FastAPI dashboard wiring."""

from pathlib import Path

import pytest


def test_dashboard_imports() -> None:
    """Dashboard core symbols are importable."""
    from src.dashboard.app import ConnectionManager, MigrationMetrics, MigrationProgress, app

    assert app is not None
    assert ConnectionManager is not None
    assert MigrationMetrics is not None
    assert MigrationProgress is not None


@pytest.mark.parametrize("action", ["stop", "pause", "resume"])
def test_invalid_controls_report_client_error(action, monkeypatch):
    from fastapi.testclient import TestClient

    from src.dashboard.app import app, migration_state

    monkeypatch.setitem(migration_state, "is_running", False)
    with TestClient(app) as client:
        response = client.post(f"/api/migration/{action}")
        assert response.status_code == 400
        assert "400" in client.get("/openapi.json").json()["paths"][f"/api/migration/{action}"]["post"]["responses"]


def test_dashboard_routes() -> None:
    """All expected dashboard routes are registered."""
    from src.dashboard.app import app

    routes = {getattr(route, "path", "") for route in app.routes}
    expected = {
        "/",
        "/ws/progress",
        "/ws/dashboard",
        "/api/progress",
        "/api/metrics",
        "/api/metrics/csv",
        "/api/migration/status",
        "/api/migration/start",
        "/api/migration/stop",
    }
    missing = expected - routes
    assert not missing, f"Missing dashboard routes: {sorted(missing)}"


def test_dashboard_templates_present() -> None:
    """Dashboard HTML template and static assets exist on disk."""
    project_root = Path(__file__).resolve().parent.parent
    assert (project_root / "src" / "dashboard" / "templates" / "dashboard.html").exists()
    assert (project_root / "src" / "dashboard" / "static" / "css" / "dashboard.css").exists()
    assert (project_root / "src" / "dashboard" / "static" / "js" / "dashboard.js").exists()


@pytest.mark.asyncio
async def test_dashboard_api() -> None:
    """Core dashboard API endpoints respond successfully."""
    from fastapi.testclient import TestClient

    from src.dashboard.app import app

    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/migration/status").status_code == 200
        assert client.get("/api/metrics").status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [None, "redis://custom-cache.invalid:6390/1"])
async def test_dashboard_lifespan_uses_configured_cache(monkeypatch, url):
    from importlib import import_module
    from unittest.mock import AsyncMock, Mock

    module = import_module("src.dashboard.app")
    if url is None:
        monkeypatch.delenv("REDIS_URL", raising=False)
    else:
        monkeypatch.setenv("REDIS_URL", url)
    cache = AsyncMock()
    connect = Mock(return_value=cache)
    monkeypatch.setattr(module.redis.Redis, "from_url", connect)
    monkeypatch.setattr(module, "collect_system_metrics", AsyncMock())
    monkeypatch.setattr(module, "update_migration_progress", AsyncMock())
    async with module.lifespan(module.app):
        connect.assert_called_once_with(url or "redis://localhost:6379", decode_responses=True)
        cache.ping.assert_awaited_once_with()
    cache.aclose.assert_awaited_once_with()
