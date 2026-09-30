from __future__ import annotations

import json

from litestar.testing import TestClient
from pydantic import SecretStr

from identity_hub_admin.app import create_admin_app
from identity_hub_admin.demo import create_demo_hub
from identity_hub_admin.settings import AdminSettings, Environment


def test_event_page_detail_validation_and_scope() -> None:
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
        response = client.get(
            "/api/v1/spaces/demo-large/events?group=identity&severity=info&limit=10",
            headers=headers,
        )
        invalid_cursor = client.get(
            "/api/v1/spaces/demo-large/events?cursor=not-valid!",
            headers=headers,
        )
        invalid_group = client.get(
            "/api/v1/spaces/demo-large/events?group=made-up",
            headers=headers,
        )
        missing = client.get(
            "/api/v1/spaces/demo-large/events/00000000-0000-0000-0000-000000000001",
            headers=headers,
        )
        hidden = client.get("/api/v1/spaces/demo-empty/events", headers=headers)

    assert response.status_code == 200
    data = response.json()["data"]
    assert len(data["items"]) == 10
    assert all(item["group"] == "identity" for item in data["items"])
    assert all(item["severity"] == "info" for item in data["items"])
    assert all("password" not in json.dumps(item) for item in data["items"])
    assert "journal" in data["groups"]
    assert any(item["type"] == "lease.acquired" and not item["external"] for item in data["types"])
    assert invalid_cursor.status_code == 400
    assert invalid_group.status_code == 400
    assert missing.status_code == 404
    assert hidden.status_code == 404
