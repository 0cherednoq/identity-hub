from __future__ import annotations

import json

from litestar.testing import TestClient
from pydantic import SecretStr

from identity_hub_admin.app import create_admin_app
from identity_hub_admin.demo import create_demo_hub
from identity_hub_admin.settings import AdminSettings, Environment


def test_background_never_claims_health_without_heartbeat() -> None:
    settings = AdminSettings(
        environment=Environment.TEST,
        demo_mode=True,
        run_dispatcher=False,
        bearer_tokens=SecretStr(
            json.dumps(
                {
                    "viewer-token": {
                        "actor": "test:viewer",
                        "role": "viewer",
                        "allowed_spaces": ["demo-large"],
                    }
                }
            )
        ),
    )
    with TestClient(create_admin_app(settings, hub=create_demo_hub())) as client:
        response = client.get("/api/v1/background", headers={"Authorization": "Bearer viewer-token"})
    assert response.status_code == 200
    processes = response.json()["data"]["processes"]
    assert {item["name"] for item in processes} >= {
        "webhooks",
        "notifications",
        "maintenance:normalize",
        "stale-probes",
    }
    assert all(item["status"] != "healthy" for item in processes if not item["heartbeat_available"])
    assert "secret" not in json.dumps(processes).lower()
