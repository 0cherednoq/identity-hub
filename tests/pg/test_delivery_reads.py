"""A2.10: safe scoped delivery journals and mutation-free previews."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub.enums import Severity
from identity_hub.hub import IdentityHub, SpaceHandle
from identity_hub.postgres.models import (
    EventCursorRecord,
    EventRecord,
    NotificationRecord,
    WebhookDeliveryRecord,
)

pytestmark = pytest.mark.pg


async def test_delivery_center_redacts_scopes_paginates_and_does_not_mutate(
    hub: IdentityHub,
    space: SpaceHandle,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    channel = await space.notifications.create_telegram_channel(
        "Operations",
        bot_token=SecretStr("telegram-token-must-not-leak"),
        chat_id=-100123456789,
        thread_id=7,
    )
    rule = await space.notifications.create_rule(
        name="Registrations",
        channel_id=channel.id,
        event_types=["identity.registered"],
        min_severity=Severity.INFO,
        created_by="admin:test",
    )
    endpoint = await space.webhooks.create(
        url="https://hooks.example.test/private/receiver?token=url-secret",
        secret=SecretStr("webhook-secret-must-not-leak"),
        event_types=["identity.registered"],
    )
    other = hub.space("delivery-other", "Delivery other")
    other_endpoint = await other.webhooks.create(
        url="https://other.example.test/hidden",
        secret=SecretStr("other-secret"),
        event_types=["identity.registered"],
    )
    ref = await space.ref()
    other_ref = await other.ref()
    now = hub.clock.now()
    event_ids = [uuid4(), uuid4()]
    async with sessionmaker.begin() as session:
        session.add_all(
            [
                EventRecord(
                    event_id=event_ids[0],
                    type="identity.registered",
                    severity="info",
                    space_id=ref.id,
                    service="fake_tg",
                    actor="test",
                    data={"status": "ready"},
                    external=True,
                    occurred_at=now,
                ),
                WebhookDeliveryRecord(
                    endpoint_id=endpoint.id,
                    event_id=event_ids[0],
                    status="failed",
                    attempt=2,
                    next_attempt_at=now + timedelta(minutes=1),
                    error="bearer raw-webhook-error-secret",
                    created_at=now,
                ),
                WebhookDeliveryRecord(
                    endpoint_id=endpoint.id,
                    event_id=event_ids[1],
                    status="pending",
                    attempt=0,
                    next_attempt_at=now,
                    created_at=now - timedelta(seconds=1),
                ),
                WebhookDeliveryRecord(
                    endpoint_id=other_endpoint.id,
                    event_id=uuid4(),
                    status="pending",
                    attempt=0,
                    next_attempt_at=now,
                    created_at=now,
                ),
                NotificationRecord(
                    rule_id=rule.id,
                    channel_id=channel.id,
                    event_ids=[event_ids[0]],
                    status="failed",
                    attempt=1,
                    scheduled_at=now,
                    error="token raw-notification-error-secret",
                ),
                NotificationRecord(
                    rule_id=rule.id,
                    channel_id=channel.id,
                    event_ids=[event_ids[1]],
                    status="pending",
                    attempt=0,
                    scheduled_at=now - timedelta(seconds=1),
                ),
                EventCursorRecord(consumer="webhooks", last_id=41),
                EventCursorRecord(consumer="notifications", last_id=42),
            ]
        )
        assert ref.id != other_ref.id

    async with sessionmaker() as session:
        cursors_before = dict(
            (await session.execute(select(EventCursorRecord.consumer, EventCursorRecord.last_id))).all()
        )
        deliveries_before = (
            await session.execute(select(func.count()).select_from(WebhookDeliveryRecord))
        ).scalar_one()
        notifications_before = (
            await session.execute(select(func.count()).select_from(NotificationRecord))
        ).scalar_one()

    first = await space.delivery_center(webhook_limit=1, notification_limit=1)
    second_webhook = await space.delivery_center(
        webhook_limit=1,
        webhook_before=first.webhook_deliveries.next_before,
        notification_limit=1,
        notification_before=first.notifications.next_before,
    )

    assert first.rules[0].preview_24h == 1
    assert first.channels[0].masked_target.endswith("6789")
    assert first.webhook_endpoints[0].masked_target == "https://hooks.example.test/…"
    assert first.webhook_deliveries.next_before is not None
    assert first.notifications.next_before is not None
    assert first.webhook_deliveries.items[0].id != second_webhook.webhook_deliveries.items[0].id
    assert first.notifications.items[0].id != second_webhook.notifications.items[0].id
    rendered = repr(first)
    for secret in (
        "telegram-token-must-not-leak",
        "webhook-secret-must-not-leak",
        "url-secret",
        "raw-webhook-error-secret",
        "raw-notification-error-secret",
        "other.example.test",
    ):
        assert secret not in rendered

    async with sessionmaker() as session:
        cursors_after = dict(
            (await session.execute(select(EventCursorRecord.consumer, EventCursorRecord.last_id))).all()
        )
        deliveries_after = (
            await session.execute(select(func.count()).select_from(WebhookDeliveryRecord))
        ).scalar_one()
        notifications_after = (
            await session.execute(select(func.count()).select_from(NotificationRecord))
        ).scalar_one()
    assert cursors_after == cursors_before
    assert deliveries_after == deliveries_before
    assert notifications_after == notifications_before
