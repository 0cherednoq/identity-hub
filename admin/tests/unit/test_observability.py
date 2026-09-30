from __future__ import annotations

import json
import logging

from _pytest.logging import LogCaptureFixture
from litestar import get
from litestar.exceptions import NotFoundException
from litestar.testing import TestClient
from pydantic import SecretStr
from pytest import MonkeyPatch

from identity_hub_admin.api.routes import route_meta
from identity_hub_admin.app import create_admin_app
from identity_hub_admin.observability import ACCESS_LOGGER, scrub_secrets
from identity_hub_admin.settings import AdminSettings


def test_scrubber_recursively_removes_secret_values() -> None:
    source = {
        "username": "operator",
        "password": "never-log-me",
        "nested": {"authorization": "Bearer secret", "safe": 3},
    }

    assert scrub_secrets(source) == {
        "username": "operator",
        "password": "[REDACTED]",
        "nested": {"authorization": "[REDACTED]", "safe": 3},
    }


def test_request_id_actor_space_status_and_latency_are_logged(monkeypatch: MonkeyPatch) -> None:
    settings = AdminSettings(
        bearer_tokens=SecretStr(
            json.dumps(
                {
                    "test-token": {
                        "actor": "service:browser-test",
                        "role": "viewer",
                        "allowed_spaces": ["alpha"],
                    }
                }
            )
        )
    )
    messages: list[str] = []

    def capture(_level: int, message: str) -> None:
        messages.append(message)

    monkeypatch.setattr(ACCESS_LOGGER, "log", capture)
    with TestClient(create_admin_app(settings)) as client:
        response = client.get(
            "/api/v1/spaces/alpha/meta?token=must-not-appear",
            headers={
                "Authorization": "Bearer test-token",
                "X-Request-ID": "browser-trace-001",
            },
        )

    assert response.status_code == 503
    records = [json.loads(message) for message in messages]
    event = next(item for item in records if item["request_id"] == "browser-trace-001")
    assert event == {
        "event": "admin_http_request",
        "request_id": "browser-trace-001",
        "actor": "service:browser-test",
        "method": "GET",
        "route": "/api/v1/spaces/alpha/meta",
        "space": "alpha",
        "status": 503,
        "latency_ms": event["latency_ms"],
    }
    assert isinstance(event["latency_ms"], float)
    access_text = "\n".join(messages)
    assert "must-not-appear" not in access_text
    assert "test-token" not in access_text


def test_diagnostics_exposes_request_id_and_non_secret_metrics() -> None:
    with TestClient(create_admin_app()) as client:
        client.get("/health/live", headers={"X-Request-ID": "before-diagnostics"})
        response = client.get(
            "/health/diagnostics",
            headers={"X-Request-ID": "diagnostics-001"},
        )

    assert response.status_code == 200
    assert response.json()["request_id"] == "diagnostics-001"
    assert response.json()["metrics"]["requests_total"] >= 1
    assert "secrets" not in response.text.lower()


def test_controlled_http_error_does_not_emit_error_stack(caplog: LogCaptureFixture) -> None:
    @get("/controlled", opt=route_meta(auth_required=False), sync_to_thread=False)
    def controlled() -> None:
        raise NotFoundException("controlled missing resource")

    with (
        caplog.at_level(logging.DEBUG),
        TestClient(create_admin_app(route_handlers=(controlled,))) as client,
    ):
        response = client.get("/controlled", headers={"X-Request-ID": "controlled-404"})

    assert response.status_code == 404
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
