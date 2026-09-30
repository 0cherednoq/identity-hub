from __future__ import annotations

import json

from litestar.testing import TestClient
from pydantic import SecretStr

from identity_hub_admin.app import create_admin_app
from identity_hub_admin.demo import create_demo_hub
from identity_hub_admin.settings import AdminSettings, Environment


def _settings() -> AdminSettings:
    return AdminSettings(
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


def test_analytics_catalog_series_and_etag_revalidation_are_scoped() -> None:
    headers = {"Authorization": "Bearer viewer-token", "X-Request-ID": "analytics-001"}
    params = {
        "metric": "identities.added",
        "start": "2026-01-01T00:00:00Z",
        "end": "2026-01-03T00:00:00Z",
        "bucket": "1d",
        "group_by": "service",
    }
    with TestClient(create_admin_app(_settings(), hub=create_demo_hub())) as client:
        catalog = client.get("/api/v1/spaces/demo-large/analytics/catalog", headers=headers)
        series = client.get("/api/v1/spaces/demo-large/analytics/series", headers=headers, params=params)
        not_modified = client.get(
            "/api/v1/spaces/demo-large/analytics/series",
            headers={**headers, "If-None-Match": series.headers["ETag"]},
            params=params,
        )
        hidden = client.get("/api/v1/spaces/demo-empty/analytics/catalog", headers=headers)

    assert catalog.status_code == 200
    assert catalog.json()["data"]["max_points"] == 1000
    added = next(item for item in catalog.json()["data"]["metrics"] if item["metric"] == "identities.added")
    assert added["dimensions"] == ["method", "service"]
    assert series.status_code == 200
    assert series.headers["Cache-Control"] == "private, max-age=0, must-revalidate"
    assert len(series.json()["data"]["groups"][0]["points"]) == 2
    assert not_modified.status_code == 304
    assert not not_modified.content
    assert hidden.status_code == 404


def test_analytics_api_rejects_unknown_dimension() -> None:
    headers = {"Authorization": "Bearer viewer-token", "X-Request-ID": "analytics-002"}
    with TestClient(create_admin_app(_settings(), hub=create_demo_hub())) as client:
        response = client.get(
            "/api/v1/spaces/demo-large/analytics/series",
            headers=headers,
            params={
                "metric": "identities.added",
                "start": "2026-01-01T00:00:00Z",
                "end": "2026-01-03T00:00:00Z",
                "bucket": "1d",
                "group_by": "identity_id",
            },
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_failed"
