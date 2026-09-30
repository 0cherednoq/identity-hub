from __future__ import annotations

import json

from litestar.testing import TestClient
from pydantic import SecretStr

from identity_hub_admin.app import create_admin_app
from identity_hub_admin.demo import create_demo_hub
from identity_hub_admin.settings import AdminSettings, Environment


def test_service_catalog_is_safe_deterministic_and_scope_counted() -> None:
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
    headers = {"Authorization": "Bearer viewer-token"}
    with TestClient(create_admin_app(settings, hub=create_demo_hub())) as client:
        response = client.get("/api/v1/services", headers=headers)
        missing = client.get("/api/v1/services/missing", headers=headers)

    assert response.status_code == 200
    items = response.json()["data"]["items"]
    assert [item["name"] for item in items] == sorted(item["name"] for item in items)
    assert all(space["space_slug"] == "demo-large" for item in items for space in item["spaces"])
    assert all(
        [operation["kind"] for operation in item["operations"]]
        == sorted(operation["kind"] for operation in item["operations"])
        for item in items
    )
    rendered = json.dumps(items)
    assert "SecretStr" not in rendered
    assert "callable" not in rendered
    assert "function" not in rendered
    assert missing.status_code == 404
