from __future__ import annotations

import json

from litestar.testing import TestClient
from pydantic import SecretStr

from identity_hub_admin.app import create_admin_app
from identity_hub_admin.demo import create_demo_hub
from identity_hub_admin.settings import AdminSettings, Environment


def test_account_card_sections_are_scoped_and_secret_free() -> None:
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
        listing = client.get(
            "/api/v1/spaces/demo-large/accounts?limit=1",
            headers=headers,
        ).json()["data"]
        identity_id = listing["items"][0]["id"]
        detail = client.get(f"/api/v1/spaces/demo-large/accounts/{identity_id}", headers=headers)
        sections = {
            section: client.get(
                f"/api/v1/spaces/demo-large/accounts/{identity_id}/{section}",
                headers=headers,
            )
            for section in (
                "state",
                "profile",
                "access",
                "verification",
                "leases",
                "network",
                "events",
                "logs",
            )
        }
        hidden = client.get(f"/api/v1/spaces/demo-empty/accounts/{identity_id}", headers=headers)
        missing = client.get(
            "/api/v1/spaces/demo-large/accounts/00000000-0000-0000-0000-000000000001",
            headers=headers,
        )

    assert detail.status_code == 200
    assert detail.json()["data"]["revision"] >= 0
    assert all(response.status_code == 200 for response in sections.values())
    regions = sections["state"].json()["data"]["data"]["regions"]
    assert regions
    assert all("retryable" in region for region in regions)
    assert sections["state"].json()["data"]["actions"] == []
    assert sections["verification"].json()["data"]["actions"] == []
    assert sections["access"].json()["data"]["data"]["secret_values_included"] is False
    assert sections["events"].json()["data"]["available"] is False
    serialized = json.dumps(
        {"detail": detail.json(), "sections": {key: value.json() for key, value in sections.items()}}
    )
    for forbidden in ("ciphertext", "wrapped_dek", "nonce", "lease_token", "context_key"):
        assert forbidden not in serialized
    assert hidden.status_code == 404
    assert missing.status_code == 404
