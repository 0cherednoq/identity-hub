from __future__ import annotations

import json

from litestar.testing import TestClient
from pydantic import SecretStr

from identity_hub_admin.app import create_admin_app
from identity_hub_admin.demo import create_demo_hub
from identity_hub_admin.settings import AdminSettings, Environment


def test_human_queue_contract_filters_cursor_and_scope() -> None:
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
            "/api/v1/spaces/demo-large/human-queue?kind=requirement&urgency=high&limit=10",
            headers=headers,
        )
        invalid_cursor = client.get(
            "/api/v1/spaces/demo-large/human-queue?cursor=not-valid-base64!",
            headers=headers,
        )
        hidden = client.get("/api/v1/spaces/demo-empty/human-queue", headers=headers)

    assert response.status_code == 200
    assert response.json()["data"] == {
        "items": [],
        "next_cursor": None,
        "counts": {
            "challenge": 0,
            "drift": 0,
            "manual_review": 0,
            "quarantine": 0,
            "requirement": 0,
        },
    }
    assert invalid_cursor.status_code == 400
    assert hidden.status_code == 404
