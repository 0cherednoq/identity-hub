from __future__ import annotations

import json

from litestar.testing import TestClient
from pydantic import SecretStr

from identity_hub_admin.app import create_admin_app
from identity_hub_admin.demo import create_demo_hub
from identity_hub_admin.settings import AdminSettings, Environment


def test_delivery_center_is_scoped_redacted_and_validates_cursors() -> None:
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
            "/api/v1/spaces/demo-large/delivery-center?webhook_limit=1&notification_limit=1",
            headers=headers,
        )
        invalid_webhook_cursor = client.get(
            "/api/v1/spaces/demo-large/delivery-center?webhook_cursor=not-valid!",
            headers=headers,
        )
        invalid_notification_cursor = client.get(
            "/api/v1/spaces/demo-large/delivery-center?notification_cursor=not-valid!",
            headers=headers,
        )
        hidden = client.get(
            "/api/v1/spaces/demo-empty/delivery-center",
            headers=headers,
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["preview_mutates_state"] is False
    assert data["channels"][0]["masked_target"].endswith("7890")
    assert data["rules"][0]["actions_available"] is False
    assert data["webhook_endpoints"][0]["masked_target"] == "https://hooks.example.test/…"
    rendered = response.text
    assert "demo-only-telegram-token" not in rendered
    assert "demo-only-webhook-secret" not in rendered
    assert "private/demo" not in rendered
    assert "token=demo-secret" not in rendered
    assert invalid_webhook_cursor.status_code == 400
    assert invalid_notification_cursor.status_code == 400
    assert hidden.status_code == 404
