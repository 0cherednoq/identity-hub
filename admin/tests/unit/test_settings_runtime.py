from __future__ import annotations

from pathlib import Path

import pytest
from litestar.testing import TestClient
from pydantic import ValidationError

from identity_hub_admin.app import create_admin_app
from identity_hub_admin.runtime import runtime_lifespan
from identity_hub_admin.settings import AdminSettings, Environment


def test_settings_normalize_base_path_and_log_level() -> None:
    settings = AdminSettings(base_path="/admin", log_level="warning", port=9000)

    assert settings.base_path == "/admin"
    assert settings.log_level == "WARNING"
    with pytest.raises(ValidationError):
        AdminSettings(base_path="admin/")


def test_production_settings_report_all_required_inputs(tmp_path: Path) -> None:
    settings = AdminSettings(environment=Environment.PRODUCTION, static_dir=tmp_path)

    errors = settings.startup_errors()
    assert any("DATABASE_URL" in error for error in errors)
    assert any("HUB_FACTORY" in error for error in errors)
    assert any("KEK_KEYS" in error for error in errors)
    assert any("index.html" in error for error in errors)


def test_readiness_tracks_lifespan() -> None:
    app = create_admin_app(AdminSettings(environment=Environment.TEST))

    with TestClient(app) as client:
        response = client.get("/health/ready")
        assert response.status_code == 200
        assert response.json()["checks"]["startup"] == "ready"

    assert app.state.runtime.state.ready is False
    assert app.state.runtime.state.checks["startup"] == "stopped"


@pytest.mark.asyncio
async def test_incompatible_library_version_fails_startup() -> None:
    app = create_admin_app(AdminSettings(environment=Environment.TEST, library_version_spec=">=99"))

    with pytest.raises(RuntimeError, match="does not satisfy"):
        async with runtime_lifespan(app):
            pass


def test_spa_fallback_and_asset_cache_are_safe(tmp_path: Path) -> None:
    static = tmp_path / "static"
    assets = static / "assets"
    assets.mkdir(parents=True)
    (static / "index.html").write_text("<h1>SPA</h1>", encoding="utf-8")
    (assets / "app-AbCdEf123.js").write_text("console.log('ok')", encoding="utf-8")
    app = create_admin_app(AdminSettings(environment=Environment.TEST, static_dir=static))

    with TestClient(app) as client:
        page = client.get("/spaces/alpha/accounts")
        asset = client.get("/assets/app-AbCdEf123.js")
        missing_api = client.get("/api/v1/missing")
        traversal = client.get("/assets/%2E%2E/index.html")

    assert page.status_code == 200
    assert page.headers["Cache-Control"] == "no-cache"
    assert page.headers["Content-Type"].startswith("text/html")
    assert asset.status_code == 200
    assert asset.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert missing_api.status_code == 404
    assert traversal.status_code == 404
