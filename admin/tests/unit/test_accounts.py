from __future__ import annotations

import json

from litestar.testing import TestClient
from pydantic import SecretStr

from identity_hub_admin.app import create_admin_app
from identity_hub_admin.demo import create_demo_hub
from identity_hub_admin.settings import AdminSettings, Environment


def test_accounts_are_scoped_secret_free_filtered_and_keyset_paginated() -> None:
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
    headers = {"Authorization": "Bearer viewer-token", "X-Request-ID": "accounts-001"}

    with TestClient(create_admin_app(settings, hub=create_demo_hub())) as client:
        first = client.get(
            "/api/v1/spaces/demo-large/accounts?lifecycle=ready&limit=20",
            headers=headers,
        )
        first_body = first.json()["data"]
        second = client.get(
            "/api/v1/spaces/demo-large/accounts",
            params={"lifecycle": "ready", "limit": 20, "cursor": first_body["next_cursor"]},
            headers=headers,
        )
        sorted_first = client.get(
            "/api/v1/spaces/demo-large/accounts",
            params={"sort": "external_ref", "direction": "asc", "limit": 2},
            headers=headers,
        )
        sorted_body = sorted_first.json()["data"]
        sorted_second = client.get(
            "/api/v1/spaces/demo-large/accounts",
            params={
                "sort": "external_ref",
                "direction": "asc",
                "limit": 2,
                "cursor": sorted_body["next_cursor"],
            },
            headers=headers,
        )
        invalid_sort = client.get(
            "/api/v1/spaces/demo-large/accounts?sort=secret",
            headers=headers,
        )
        hidden = client.get("/api/v1/spaces/demo-empty/accounts", headers=headers)

    assert first.status_code == 200
    assert first_body["total"] == 215
    assert first_body["total_label"] == "215"
    assert len(first_body["items"]) == 20
    assert second.status_code == 200
    second_body = second.json()["data"]
    first_ids = {item["id"] for item in first_body["items"]}
    second_ids = {item["id"] for item in second_body["items"]}
    assert first_ids.isdisjoint(second_ids)
    assert [item["external_ref"] for item in sorted_body["items"]] == [
        "demo-large-0001",
        "demo-large-0002",
    ]
    assert [item["external_ref"] for item in sorted_second.json()["data"]["items"]] == [
        "demo-large-0003",
        "demo-large-0004",
    ]
    assert invalid_sort.status_code == 400
    serialized = json.dumps(first_body)
    assert "token" not in serialized
    assert "credentials" not in serialized
    assert hidden.status_code == 404
    assert hidden.json()["error"]["code"] == "not_found"
