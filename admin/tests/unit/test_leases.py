from __future__ import annotations

import json

from litestar.testing import TestClient
from pydantic import SecretStr

from identity_hub_admin.app import create_admin_app
from identity_hub_admin.demo import create_demo_hub
from identity_hub_admin.settings import AdminSettings, Environment


def test_lease_dashboard_history_and_scope() -> None:
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
        dashboard = client.get(
            "/api/v1/spaces/demo-large/leases?group_by=holder",
            headers=headers,
        )
        history = client.get(
            "/api/v1/spaces/demo-large/leases/history?limit=10",
            headers=headers,
        )
        invalid = client.get(
            "/api/v1/spaces/demo-large/leases/history?cursor=not-valid!",
            headers=headers,
        )
        hidden = client.get("/api/v1/spaces/demo-empty/leases", headers=headers)

    assert dashboard.status_code == 200
    data = dashboard.json()["data"]
    assert [item["window"] for item in data["windows"]] == ["15m", "1h", "6h", "24h"]
    assert data["group_by"] == "holder"
    assert data["empty_pick_metrics_available"] is False
    assert history.status_code == 200
    assert history.json()["data"] == {"items": [], "next_cursor": None}
    assert invalid.status_code == 400
    assert hidden.status_code == 404
