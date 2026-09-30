from __future__ import annotations

from pathlib import Path

import pytest
from litestar.testing import TestClient

from identity_hub_admin.app import create_admin_app
from identity_hub_admin.demo import DEMO_SPACES, create_demo_hub, demo_counts, seed_demo
from identity_hub_admin.settings import AdminSettings, Environment


@pytest.mark.asyncio
async def test_demo_seed_is_idempotent_and_contains_all_scenarios() -> None:
    hub = create_demo_hub()
    await hub.start()

    first = await seed_demo(hub, Environment.TEST)
    second = await seed_demo(hub, Environment.TEST)
    counts = await demo_counts(hub)

    expected_accounts = sum(space.account_count for space in DEMO_SPACES)
    assert first.accounts_created == expected_accounts
    assert first.accounts_existing == 0
    assert second.accounts_created == 0
    assert second.accounts_existing == expected_accounts
    assert counts == {space.slug: space.account_count for space in DEMO_SPACES}


@pytest.mark.asyncio
async def test_demo_seed_refuses_production() -> None:
    with pytest.raises(RuntimeError, match="forbidden in production"):
        await seed_demo(create_demo_hub(), Environment.PRODUCTION)


def test_demo_catalog_is_available_only_when_enabled() -> None:
    plain_app = create_admin_app(AdminSettings(environment=Environment.TEST))
    demo_app = create_admin_app(
        AdminSettings(environment=Environment.TEST, demo_mode=True, run_dispatcher=False),
        hub=create_demo_hub(),
    )

    with TestClient(plain_app) as client:
        assert client.get("/api/v1/demo/scenarios").status_code == 404

    with TestClient(demo_app) as client:
        response = client.get("/api/v1/demo/scenarios")

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["credentials"] == "none"
    assert {item["state"] for item in body["scenarios"]} == {"loading", "empty", "error", "large"}
    assert next(item for item in body["scenarios"] if item["state"] == "large")["account_count"] == 250


def test_production_configuration_rejects_demo_mode(tmp_path: Path) -> None:
    settings = AdminSettings(
        environment=Environment.PRODUCTION,
        demo_mode=True,
        static_dir=tmp_path,
    )

    assert "IH_ADMIN_DEMO_MODE is forbidden in production" in settings.startup_errors()
