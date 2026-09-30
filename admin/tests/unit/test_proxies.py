from __future__ import annotations

import json

from litestar.testing import TestClient
from pydantic import SecretStr

from identity_hub_admin.app import create_admin_app
from identity_hub_admin.demo import create_demo_hub
from identity_hub_admin.settings import AdminSettings, Environment


def test_proxy_overview_detail_cursor_and_scope() -> None:
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
            "/api/v1/spaces/demo-large/proxies?capacity=available&limit=10",
            headers=headers,
        )
        invalid = client.get(
            "/api/v1/spaces/demo-large/proxies?cursor=not-valid!",
            headers=headers,
        )
        missing = client.get(
            "/api/v1/spaces/demo-large/proxies/00000000-0000-0000-0000-000000000001",
            headers=headers,
        )
        hidden = client.get("/api/v1/spaces/demo-empty/proxies", headers=headers)

    assert response.status_code == 200
    assert response.json()["data"] == {"items": [], "pools": [], "next_cursor": None}
    assert invalid.status_code == 400
    assert missing.status_code == 404
    assert hidden.status_code == 404
