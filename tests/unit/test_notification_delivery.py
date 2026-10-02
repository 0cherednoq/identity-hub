"""F3.3: доставка уведомлений Telegram под лимитом транспорта (R2-F03, R2-S19, R2-F25)."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import SecretStr

from identity_hub import IdentityCreate, IdentityHub, Override, StaticSecretsProvider, ValidationFailed
from identity_hub.clock import FakeClock
from identity_hub.dispatch.notifications import (
    TELEGRAM_MESSAGE_LIMIT,
    TelegramRateLimited,
    TelegramRejected,
    _render_messages,
)
from identity_hub.events import Event
from identity_hub.testing import MemoryStore
from tests.fixtures.services import FAKE_TG

pytestmark = pytest.mark.unit


class LimitedTelegram:
    """Имитация Bot API: 20 сообщений в минуту в чат, 4096 символов в сообщении."""

    def __init__(self, clock: FakeClock, *, per_minute: int = 20) -> None:
        self.clock = clock
        self.per_minute = per_minute
        self.sent: list[tuple[datetime, str]] = []
        self.calls = 0
        self.too_long = 0

    async def send_message(
        self, bot_token: SecretStr, chat_id: int | str, text: str, *, thread_id: int | None = None
    ) -> None:
        self.calls += 1
        if len(text) > TELEGRAM_MESSAGE_LIMIT:
            self.too_long += 1
            raise TelegramRejected(400, "HTTP 400: Bad Request: message is too long")
        now = self.clock.now()
        recent = [at for at, _ in self.sent if (now - at).total_seconds() < 60]
        if len(recent) >= self.per_minute:
            raise TelegramRateLimited(60 - (now - recent[0]).total_seconds() + 1)
        self.sent.append((now, text))


async def _banned(
    count: int, *, digest: int | None = None
) -> tuple[IdentityHub, FakeClock, LimitedTelegram, Any]:
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    telegram = LimitedTelegram(clock)
    hub = IdentityHub(
        secrets=StaticSecretsProvider({"k1": b"\x33" * 32}, "k1"),
        services=[FAKE_TG],
        store=MemoryStore(),
        clock=clock,
        telegram_transport=telegram,
    )
    await hub.start()
    space = hub.space("notify", "Notify")
    channel = await space.notifications.create_telegram_channel(
        "ops", bot_token=SecretStr("123:abc"), chat_id=-100
    )
    await space.notifications.create_rule(
        name="bans",
        channel_id=channel.id,
        event_types=["identity.standing.changed"],
        digest_seconds=digest,
        created_by="op",
    )
    created = await space.identities.create_many(
        [IdentityCreate(service="fake_tg", external_ref=f"+{n}") for n in range(count)]
    )
    for identity in created.created:
        await space.identities.override(
            identity.id, Override("standing", "", "banned", reason="mass ban", actor="platform")
        )
    return hub, clock, telegram, space


async def _deliveries(space: Any) -> list[Any]:
    """Все доставки уведомлений пространства (read-модель отдаёт страницы до 200 строк)."""
    items: list[Any] = []
    before = None
    while True:
        page = (
            await space.admin.delivery_center(notification_limit=200, notification_before=before)
        ).notifications
        items.extend(page.items)
        before = page.next_before
        if before is None:
            return items


async def _statuses(space: Any) -> Counter[str]:
    return Counter(item.status for item in await _deliveries(space))


async def test_burst_is_delivered_without_losses_under_rate_limit() -> None:
    """R2-F03: 300 событий при лимите 20 сообщений в минуту — доставлены все, `dead` нет."""
    hub, clock, telegram, space = await _banned(300)

    rounds = 0
    while len(telegram.sent) < 300 and rounds < 40:
        await hub.dispatcher.drain()
        clock.advance(65)
        rounds += 1

    assert await _statuses(space) == {"succeeded": 300}
    assert len(telegram.sent) == 300
    # 429 останавливает отправку в канал до конца паузы: не больше одного лишнего вызова на круг
    assert telegram.calls <= 300 + rounds
    deliveries = await _deliveries(space)
    assert {item.attempt for item in deliveries} == {1}  # 429 попыткой не считается
    assert all(item.sent_at is not None for item in deliveries)


async def test_large_digest_is_delivered_in_parts() -> None:
    """R2-F03: дайджест на 150 событий режется на сообщения до 4096 символов."""
    hub, clock, telegram, space = await _banned(150, digest=300)

    await hub.dispatcher.drain()  # события разложены в одно окно дайджеста, оно ещё не закрыто
    for _ in range(10):
        clock.advance(301)
        await hub.dispatcher.drain()

    assert await _statuses(space) == {"succeeded": 1}
    assert telegram.too_long == 0 and len(telegram.sent) > 1
    texts = [text for _, text in telegram.sent]
    assert all(len(text) <= TELEGRAM_MESSAGE_LIMIT for text in texts)
    assert sum(text.count("identity.standing.changed") for text in texts) == 150
    assert texts[0].startswith("Сводка Identity Hub (150)")


async def test_digest_parts_already_sent_are_not_repeated_after_rate_limit() -> None:
    hub, clock, telegram, space = await _banned(150, digest=300)
    telegram.per_minute = 2  # лимит наступает посреди дайджеста

    await hub.dispatcher.drain()  # события разложены в одно окно дайджеста, оно ещё не закрыто
    for _ in range(10):
        clock.advance(301)
        await hub.dispatcher.drain()

    assert await _statuses(space) == {"succeeded": 1}
    texts = [text for _, text in telegram.sent]
    assert len(texts) == len(set(texts))  # ни одна часть не ушла дважды
    assert sum(text.count("identity.standing.changed") for text in texts) == 150


def _event(n: int) -> Event:
    from uuid import uuid4

    from identity_hub.enums import Severity

    return Event(
        n,
        uuid4(),
        "identity.standing.changed",
        Severity.WARNING,
        datetime(2026, 1, 1, tzinfo=UTC),
        "s",
        "fake_tg",
        uuid4(),
        None,
        None,
        n,
        "platform",
        {"from": "good", "to": "banned"},
    )


def test_single_message_is_truncated_with_ellipsis() -> None:
    template = "{type} " + "x" * 4090
    (message,) = _render_messages((_event(1),), template, None)
    assert len(message) == TELEGRAM_MESSAGE_LIMIT and message.endswith("…")

    astral = "{type} " + "😀" * 3000  # лимит Telegram считается в единицах UTF-16
    (message,) = _render_messages((_event(1),), astral, None)
    assert len(message.encode("utf-16-le")) // 2 <= TELEGRAM_MESSAGE_LIMIT and message.endswith("…")


def test_digest_messages_keep_every_event_and_fit_the_limit() -> None:
    events = tuple(_event(n) for n in range(400))
    messages = _render_messages(events, None, "https://admin.test")
    assert len(messages) > 1
    assert all(len(m) <= TELEGRAM_MESSAGE_LIMIT for m in messages)
    assert sum(m.count("identity.standing.changed") for m in messages) == 400
    assert all(m.strip() and not m.startswith("\n") for m in messages)


@pytest.mark.parametrize(
    "template",
    ["{type:{data}}", "{type:>{severity}}", "{type.__class__}", "{data[token]}", "{0}", "{}"],
)
async def test_template_rejects_nested_and_unknown_fields(template: str) -> None:
    """R2-S19: вложенное поле в format-spec не обходит проверку имён."""
    _hub, _clock, _telegram, space = await _banned(0)
    (channel,) = await space.notifications.channels()
    with pytest.raises(ValidationFailed):
        await space.notifications.create_rule(
            name="bad", channel_id=channel.id, event_types=["identity.*"], template=template, created_by="op"
        )
    (rule,) = await space.notifications.rules()
    with pytest.raises(ValidationFailed):
        await space.notifications.update_rule(rule.id, template=template)
    await space.notifications.update_rule(rule.id, template="{severity:>10} {type}: {link}")


async def test_update_channel_can_reset_thread_id() -> None:
    """R2-F25: `thread_id=None` сбрасывает тему, пропущенный аргумент оставляет её."""
    _hub, _clock, _telegram, space = await _banned(0)
    channel = await space.notifications.create_telegram_channel(
        "topic", bot_token=SecretStr("123:abc"), chat_id=-200, thread_id=12
    )

    async def current() -> Any:
        return next(item for item in await space.notifications.channels() if item.id == channel.id)

    await space.notifications.update_telegram_channel(channel.id, title="renamed")
    assert ((await current()).title, (await current()).thread_id) == ("renamed", 12)
    await space.notifications.update_telegram_channel(channel.id, chat_id=-300)
    assert ((await current()).chat_id, (await current()).thread_id) == (-300, 12)
    await space.notifications.update_telegram_channel(channel.id, thread_id=None)
    assert ((await current()).chat_id, (await current()).thread_id) == (-300, None)
    await space.notifications.update_telegram_channel(channel.id, thread_id=7)
    assert (await current()).thread_id == 7
