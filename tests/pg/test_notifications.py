"""P8.4: Telegram-уведомления, дайджесты и retry_after (FR-15.1–15.7)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub.dispatch.notifications import TelegramRateLimited, TelegramRejected
from identity_hub.enums import Severity
from identity_hub.events import EventDraft
from identity_hub.hub import IdentityHub, SpaceHandle
from identity_hub.postgres.models import NotificationChannelRecord, NotificationRecord

pytestmark = pytest.mark.pg


class FakeTelegramTransport:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.failures: list[Exception] = []

    async def send_message(
        self,
        bot_token: SecretStr,
        chat_id: int | str,
        text: str,
        *,
        thread_id: int | None = None,
    ) -> None:
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append(
            {
                "token": bot_token.get_secret_value(),
                "chat_id": chat_id,
                "thread_id": thread_id,
                "text": text,
            }
        )


async def _setup(
    hub: IdentityHub,
    space: SpaceHandle,
    transport: FakeTelegramTransport,
    *,
    digest_seconds: int | None = None,
) -> tuple[Any, Any]:
    hub.notification_dispatcher.transport = transport
    channel = await space.notifications.create_telegram_channel(
        "Ops", bot_token=SecretStr("123456:telegram-secret"), chat_id=-100123, thread_id=12
    )
    rule = await space.notifications.create_rule(
        name="Баны",
        channel_id=channel.id,
        event_types=["identity.standing.changed"],
        filter={"service": "fake_tg", "data.to": ["frozen", "banned"]},
        min_severity=Severity.WARNING,
        digest_seconds=digest_seconds,
        created_by="admin:test",
    )
    return channel, rule


async def _standing_event(space: SpaceHandle, identity_id: Any, target: str) -> None:
    async with space._tx() as tx:
        await space._emit(
            tx,
            EventDraft(
                "identity.standing.changed",
                actor="test",
                service="fake_tg",
                identity_id=identity_id,
                severity=Severity.WARNING,
                data={"from": "good", "to": target},
            ),
        )


async def test_immediate_notification_filters_data_and_encrypts_token(
    hub: IdentityHub,
    space: SpaceHandle,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    transport = FakeTelegramTransport()
    channel, _ = await _setup(hub, space, transport)
    identity = await space.register(service="fake_tg", external_ref="+10", labels={"farm": "3"})
    await _standing_event(space, identity.identity_id, "good")
    await _standing_event(space, identity.identity_id, "banned")

    await hub.dispatcher.drain()

    assert len(transport.sent) == 1
    sent = transport.sent[0]
    assert sent["token"] == "123456:telegram-secret"
    assert sent["chat_id"] == -100123 and sent["thread_id"] == 12
    assert "identity.standing.changed" in sent["text"]
    assert f"https://admin.test/identities/{identity.identity_id}" in sent["text"]
    assert "telegram-secret" not in sent["text"]
    async with sessionmaker() as session:
        stored = (
            await session.execute(
                select(NotificationChannelRecord).where(NotificationChannelRecord.id == channel.id)
            )
        ).scalar_one()
        notification = (await session.execute(select(NotificationRecord))).scalar_one()
    assert b"telegram-secret" not in stored.ciphertext
    assert notification.status == "succeeded" and notification.attempt == 1


async def test_digest_accumulates_one_open_notification(
    hub: IdentityHub,
    space: SpaceHandle,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    transport = FakeTelegramTransport()
    # окно заведомо длиннее подготовки под нагрузкой; срок ниже сдвигается явно
    _, rule = await _setup(hub, space, transport, digest_seconds=300)
    first = await space.register(service="fake_tg", external_ref="+11")
    second = await space.register(service="fake_tg", external_ref="+12")
    await _standing_event(space, first.identity_id, "banned")
    await _standing_event(space, second.identity_id, "frozen")

    await hub.dispatcher.run_once()  # поставить события в одно открытое окно, срок ещё не наступил
    assert transport.sent == []
    async with sessionmaker() as session, session.begin():
        rows = (await session.execute(select(NotificationRecord))).scalars().all()
        assert len(rows) == 1 and len(rows[0].event_ids) == 2
        assert rows[0].digest_key is not None and rows[0].rule_id == rule.id
        await session.execute(update(NotificationRecord).values(scheduled_at=func.now()))

    assert await hub.notification_dispatcher.deliver_due() == 1
    assert len(transport.sent) == 1
    assert transport.sent[0]["text"].count("identity.standing.changed") == 2


async def test_telegram_rejection_is_dead_without_retries(
    hub: IdentityHub,
    space: SpaceHandle,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    transport = FakeTelegramTransport()
    transport.failures.append(TelegramRejected(403, "HTTP 403: Forbidden: bot was blocked by the user"))
    await _setup(hub, space, transport)
    identity = await space.register(service="fake_tg", external_ref="+14")
    await _standing_event(space, identity.identity_id, "banned")

    await hub.dispatcher.run_once()

    async with sessionmaker() as session:
        notification = (await session.execute(select(NotificationRecord))).scalar_one()
    assert notification.status == "dead" and notification.attempt == 1
    assert notification.error == "TelegramRejected: HTTP 403: Forbidden: bot was blocked by the user"
    assert transport.sent == []


async def test_telegram_429_uses_retry_after(
    hub: IdentityHub,
    space: SpaceHandle,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    transport = FakeTelegramTransport()
    transport.failures.append(TelegramRateLimited(7))
    await _setup(hub, space, transport)
    identity = await space.register(service="fake_tg", external_ref="+13")
    await _standing_event(space, identity.identity_id, "banned")

    await hub.dispatcher.run_once()

    async with sessionmaker() as session, session.begin():
        notification = (await session.execute(select(NotificationRecord))).scalar_one()
        assert notification.status == "failed" and notification.attempt == 1
        delay = (await session.execute(select(NotificationRecord.scheduled_at - func.now()))).scalar_one()
        assert timedelta(seconds=5) <= delay <= timedelta(seconds=7)
        await session.execute(update(NotificationRecord).values(scheduled_at=func.now()))

    assert await hub.notification_dispatcher.deliver_due() == 1
    assert len(transport.sent) == 1
    async with sessionmaker() as session:
        notification = (await session.execute(select(NotificationRecord))).scalar_one()
    assert notification.status == "succeeded" and notification.attempt == 2
