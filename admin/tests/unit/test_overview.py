from __future__ import annotations

import json

from litestar.testing import TestClient
from pydantic import SecretStr

from identity_hub_admin.app import create_admin_app
from identity_hub_admin.demo import create_demo_hub
from identity_hub_admin.settings import AdminSettings, Environment


def test_overview_filters_spaces_before_aggregation_and_supports_service_slice() -> None:
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
                        "allowed_spaces": ["demo-empty", "demo-large"],
                    }
                }
            )
        ),
    )
    headers = {"Authorization": "Bearer viewer-token", "X-Request-ID": "overview-001"}

    with TestClient(create_admin_app(settings, hub=create_demo_hub())) as client:
        response = client.get("/api/v1/overview?service=demo_account", headers=headers)
        space_response = client.get("/api/v1/spaces/demo-large/overview", headers=headers)
        hidden_response = client.get("/api/v1/spaces/demo-loading/overview", headers=headers)

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["service"] == "demo_account"
    assert body["totals"]["spaces"] == 2
    assert body["totals"]["total"] == 250
    assert [item["slug"] for item in body["spaces"]] == ["demo-empty", "demo-large"]
    assert space_response.status_code == 200
    assert space_response.json()["data"]["services"][0]["regions"]["lifecycle"] == {
        "provisioning": 35,
        "ready": 215,
    }
    assert hidden_response.status_code == 404
    assert hidden_response.json()["error"]["code"] == "not_found"
