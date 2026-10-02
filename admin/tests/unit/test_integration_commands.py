from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from identity_hub.clock import FakeClock
from identity_hub.crypto import StaticSecretsProvider
from identity_hub.hub import IdentityHub
from identity_hub.store.base import WebhookDeliveryRow
from identity_hub.testing.memory import MemoryStore
from litestar.testing import TestClient
from pydantic import SecretStr
from tests.fakes import RecordingTelegramTransport

from identity_hub_admin.app import create_admin_app
from identity_hub_admin.settings import AdminSettings, Environment

SPACE = "integrations"
NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


def _settings() -> AdminSettings:
    tokens = {
        f"{role}-token": {
            "actor": f"test:{role}",
            "role": role,
            "allowed_spaces": [SPACE],
        }
        for role in ("viewer", "operator", "admin")
    }
    return AdminSettings(
        environment=Environment.TEST,
        run_dispatcher=False,
        bearer_tokens=SecretStr(json.dumps(tokens)),
    )


def _headers(role: str, key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {role}-token",
        "Idempotency-Key": key,
    }


def _hub() -> tuple[IdentityHub, RecordingTelegramTransport]:
    transport = RecordingTelegramTransport()
    clock = FakeClock(NOW)
    hub = IdentityHub(
        secrets=StaticSecretsProvider({"test": b"0123456789abcdef0123456789abcdef"}, "test"),
        store=MemoryStore(clock),
        clock=clock,
        strict_events=True,
        telegram_transport=transport,
    )
    asyncio.run(hub.space(SPACE, "Integrations").ref())
    return hub, transport


def _confirmation(code: str) -> dict[str, object]:
    return {"confirmed": True, "consequence_codes": [code]}


def test_notification_crud_test_send_validation_rbac_and_redacted_reads() -> None:
    hub, transport = _hub()
    store = cast(MemoryStore, hub._store)
    with TestClient(create_admin_app(_settings(), hub=hub)) as client:
        created = client.post(
            f"/api/v1/spaces/{SPACE}/notification-channels",
            headers=_headers("admin", "channel-create-0001"),
            json={
                "title": "Ops",
                "bot_token": "telegram-token-must-not-leak",
                "chat_id": "@identity-hub-ops",
                "reason": "Configure operator alerts",
            },
        )
        assert created.status_code == 201
        channel_id = created.json()["data"]["resource_id"]

        tested = client.post(
            f"/api/v1/spaces/{SPACE}/notification-channels/{channel_id}/commands/test",
            headers=_headers("operator", "channel-test-0001"),
            json={"text": "probe", "reason": "Validate Telegram delivery"},
        )
        disabled = client.post(
            f"/api/v1/spaces/{SPACE}/notification-channels/{channel_id}/commands/enable",
            headers=_headers("operator", "channel-disable-0001"),
            json={"enabled": False, "reason": "Pause Telegram delivery"},
        )
        rule = client.post(
            f"/api/v1/spaces/{SPACE}/notification-rules",
            headers=_headers("operator", "rule-create-0001"),
            json={
                "name": "Critical identities",
                "channel_id": channel_id,
                "event_types": ["identity.*"],
                "filter": {"service": ["telegram"]},
                "min_severity": "warning",
                "template": "[{severity}] {type}",
                "reason": "Route critical events",
            },
        )
        assert store._state.notifications == {}
        assert store._state.cursors == {}
        assert rule.status_code == 201
        rule_id = UUID(rule.json()["data"]["resource_id"])
        store._state.notifications[1] = {
            "id": 1,
            "rule_id": rule_id,
            "channel_id": UUID(channel_id),
            "event_ids": [],
            "digest_key": None,
            "scheduled_at": NOW,
            "status": "dead",
            "attempt": 4,
            "error": "TelegramRateLimited",
        }
        retry_body = {
            "confirmation": _confirmation("retries_delivery"),
            "reason": "Retry reviewed notification delivery",
        }
        notification_retry = client.post(
            f"/api/v1/spaces/{SPACE}/notification-deliveries/1/commands/retry",
            headers=_headers("operator", "notification-retry-0001"),
            json=retry_body,
        )
        notification_replay = client.post(
            f"/api/v1/spaces/{SPACE}/notification-deliveries/1/commands/retry",
            headers=_headers("operator", "notification-retry-0001"),
            json=retry_body,
        )
        bad_template = client.post(
            f"/api/v1/spaces/{SPACE}/notification-rules",
            headers=_headers("operator", "rule-create-bad-template"),
            json={
                "name": "Unsafe",
                "channel_id": channel_id,
                "event_types": ["identity.*"],
                "template": "{data.__class__}",
                "reason": "Verify template validation",
            },
        )
        viewer_forbidden = client.post(
            f"/api/v1/spaces/{SPACE}/notification-channels/{channel_id}/commands/test",
            headers=_headers("viewer", "viewer-test-0001"),
            json={"text": "probe", "reason": "Must be forbidden"},
        )
        center = client.get(
            f"/api/v1/spaces/{SPACE}/delivery-center",
            headers={"Authorization": "Bearer admin-token"},
        )

    assert tested.status_code == 201
    assert disabled.status_code == 201
    assert notification_retry.status_code == notification_replay.status_code == 201
    assert notification_retry.json() == notification_replay.json()
    assert bad_template.status_code == 422
    assert viewer_forbidden.status_code == 403
    assert [(message.chat_id, message.text) for message in transport.messages] == [
        ("@identity-hub-ops", "probe")
    ]
    assert center.status_code == 200
    rendered = center.text
    assert "telegram-token-must-not-leak" not in rendered
    assert "@identity-hub-ops" not in rendered
    assert center.json()["data"]["channels"][0]["actions_available"] is True
    assert center.json()["data"]["channels"][0]["enabled"] is False
    notification = store._state.notifications[1]
    assert notification["status"] == "pending"
    assert notification["attempt"] == 0
    retry_audits = [
        event
        for event, _, _ in store._state.events
        if event.type == "admin.integration.changed" and event.data["action"] == "retry"
    ]
    assert len(retry_audits) == 1


def test_webhook_one_time_values_ssrf_rotation_and_audited_idempotent_retry() -> None:
    hub, _ = _hub()
    store = cast(MemoryStore, hub._store)
    with TestClient(create_admin_app(_settings(), hub=hub)) as client:
        body = {
            "url": "https://hooks.example.test/private/path?token=hidden",
            "description": "Primary sink",
            "event_types": ["identity.*"],
            "filter": {"labels": {"farm": "3"}},
            "reason": "Create signed event sink",
        }
        created = client.post(
            f"/api/v1/spaces/{SPACE}/webhook-endpoints",
            headers=_headers("admin", "webhook-create-0001"),
            json=body,
        )
        assert created.status_code == 201
        endpoint_id = UUID(created.json()["data"]["resource_id"])
        signing_value = created.json()["data"]["one_time_signing_value"]
        assert len(signing_value) >= 32
        assert created.headers["cache-control"].startswith("no-store")

        consumed = client.post(
            f"/api/v1/spaces/{SPACE}/webhook-endpoints",
            headers=_headers("admin", "webhook-create-0001"),
            json=body,
        )
        private = client.post(
            f"/api/v1/spaces/{SPACE}/webhook-endpoints",
            headers=_headers("admin", "webhook-private-0001"),
            json={**body, "url": "https://127.0.0.1/admin"},
        )
        rotated = client.post(
            f"/api/v1/spaces/{SPACE}/webhook-endpoints/{endpoint_id}/commands/rotate",
            headers=_headers("admin", "webhook-rotate-0001"),
            json={
                "overlap_seconds": 7200,
                "confirmation": _confirmation("rotates_signing_key"),
                "reason": "Scheduled signing key rotation",
            },
        )
        rotated_consumed = client.post(
            f"/api/v1/spaces/{SPACE}/webhook-endpoints/{endpoint_id}/commands/rotate",
            headers=_headers("admin", "webhook-rotate-0001"),
            json={
                "overlap_seconds": 7200,
                "confirmation": _confirmation("rotates_signing_key"),
                "reason": "Scheduled signing key rotation",
            },
        )

        store._state.webhook_deliveries[1] = WebhookDeliveryRow(
            id=1,
            endpoint_id=endpoint_id,
            event_id=uuid4(),
            status="dead",
            attempt=3,
            next_attempt_at=NOW,
            response_code=503,
            response_ms=20,
            error="HTTP 503",
            created_at=NOW,
            delivered_at=None,
        )
        retry_body = {
            "confirmation": _confirmation("retries_delivery"),
            "reason": "Retry reviewed failed delivery",
        }
        retried = client.post(
            f"/api/v1/spaces/{SPACE}/webhook-deliveries/1/commands/retry",
            headers=_headers("operator", "webhook-retry-0001"),
            json=retry_body,
        )
        replay = client.post(
            f"/api/v1/spaces/{SPACE}/webhook-deliveries/1/commands/retry",
            headers=_headers("operator", "webhook-retry-0001"),
            json=retry_body,
        )
        center = client.get(
            f"/api/v1/spaces/{SPACE}/delivery-center",
            headers={"Authorization": "Bearer admin-token"},
        )

    assert consumed.status_code == 409
    assert private.status_code == 422
    assert rotated.status_code == 201
    assert rotated.json()["data"]["overlap_seconds"] == 7200
    assert rotated.json()["data"]["one_time_signing_value"] != signing_value
    assert rotated_consumed.status_code == 409
    assert retried.status_code == replay.status_code == 201
    assert retried.json() == replay.json()
    delivery = store._state.webhook_deliveries[1]
    assert delivery.status == "pending" and delivery.attempt == 0 and delivery.error is None
    audits = [event for event, _, _ in store._state.events if event.type == "admin.integration.changed"]
    assert len([event for event in audits if event.data["action"] == "retry"]) == 1
    assert "private/path" not in center.text
    assert "token=hidden" not in center.text
    assert signing_value not in center.text
