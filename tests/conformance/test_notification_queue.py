"""F3.3: очередь уведомлений одинакова на обоих хранилищах (R2-D13, R2-P13, R2-F03)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from identity_hub import IdentityHub, NotFound, SpaceHandle
from identity_hub.events import Event, EventDraft

pytestmark = pytest.mark.conformance

CLAIM = timedelta(seconds=30)
RETRY = timedelta(seconds=30)


async def _channel(space: SpaceHandle, title: str = "ops") -> UUID:
    channel = await space.notifications.create_telegram_channel(
        title, bot_token=SecretStr("123:abc"), chat_id=-100
    )
    return channel.id


async def _rule(
    space: SpaceHandle, channel_id: UUID, name: str = "updates", digest: int | None = None
) -> UUID:
    rule = await space.notifications.create_rule(
        name=name,
        channel_id=channel_id,
        event_types=["identity.updated"],
        min_severity="info",  # type: ignore[arg-type]
        digest_seconds=digest,
        created_by="op",
    )
    return rule.id


async def _events(hub: IdentityHub, space: SpaceHandle, count: int) -> list[Event]:
    """Записать события и вернуть их так, как их получает потребитель диспетчера."""
    for n in range(count):
        async with space._tx(own=True) as tx:
            await space._emit(tx, EventDraft("identity.updated", actor="test", data={"n": n}))
    captured: list[Event] = []

    async def capture(events: list[Event]) -> None:
        captured.extend(events)

    await hub._store.process_events(f"capture-{uuid4().hex}", capture, limit=1000)
    return [event for event in captured if event.type == "identity.updated" and event.space == space.slug]


async def _items(space: SpaceHandle) -> list[Any]:
    center = await space.admin.delivery_center(notification_limit=200)
    return sorted(center.notifications.items, key=lambda item: item.id)


async def _finish(hub: IdentityHub, attempt: Any, **kwargs: Any) -> bool:
    options = {"succeeded": False, "error": None, "retry_after": RETRY, "max_attempts": 10, **kwargs}
    return await hub._store.finish_notification(attempt.notification_id, attempt.attempt, **options)


async def test_replayed_batch_does_not_duplicate_immediate_notifications(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    """R2-D13: повтор пачки потребителем не создаёт второе уведомление по тому же правилу и событию."""
    await _rule(space, await _channel(space))
    events = await _events(hub_under_test, space, 3)
    dispatcher = hub_under_test._notification_dispatcher

    await dispatcher.handle_events(events)
    await dispatcher.handle_events(events)  # пачка пришла ещё раз (at-least-once)
    await dispatcher.handle_events(events[:1])

    items = await _items(space)
    assert len(items) == 3
    assert sorted(item.event_ids[0] for item in items) == sorted(event.event_id for event in events)


async def test_two_rules_get_their_own_notification_for_one_event(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    channel_id = await _channel(space)
    await _rule(space, channel_id, "first")
    await _rule(space, channel_id, "second")
    events = await _events(hub_under_test, space, 1)

    await hub_under_test._notification_dispatcher.handle_events(events)
    await hub_under_test._notification_dispatcher.handle_events(events)

    assert sorted(item.rule_name for item in await _items(space)) == ["first", "second"]


async def test_success_sets_sent_at(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    """R2-P13: `sent_at` проставляется на обоих хранилищах."""
    await _rule(space, await _channel(space))
    await hub_under_test._notification_dispatcher.handle_events(await _events(hub_under_test, space, 1))
    (attempt,) = await hub_under_test._store.claim_notifications(limit=10, claim_for=CLAIM)
    assert (await _items(space))[0].sent_at is None

    assert await _finish(hub_under_test, attempt, succeeded=True) is True
    assert await _finish(hub_under_test, attempt, succeeded=True) is False

    (item,) = await _items(space)
    assert (item.status, item.attempt) == ("succeeded", 1) and item.sent_at is not None


async def test_rate_limit_pauses_channel_without_counting_the_attempt(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    """R2-F03: 429 не расходует попытку, а канал не захватывается до конца паузы."""
    limited = await _channel(space, "limited")
    other = await _channel(space, "other")
    await _rule(space, limited, "limited-rule")
    await _rule(space, other, "other-rule")
    events = await _events(hub_under_test, space, 2)
    await hub_under_test._notification_dispatcher.handle_events(events)
    store = hub_under_test._store
    claimed = await store.claim_notifications(limit=10, claim_for=CLAIM)
    assert len(claimed) == 4
    of_limited = [item for item in claimed if item.channel_id == limited]
    of_other = [item for item in claimed if item.channel_id == other]

    for attempt in of_limited:
        assert await _finish(
            hub_under_test,
            attempt,
            error="TelegramRateLimited",
            retry_after=timedelta(minutes=5),
            pause_channel=timedelta(minutes=5),
        )
    for attempt in of_other:
        assert await _finish(hub_under_test, attempt, error="TelegramUnavailable", retry_after=timedelta(0))

    by_rule = {name: [] for name in ("limited-rule", "other-rule")}
    for item in await _items(space):
        by_rule[item.rule_name].append((item.status, item.attempt))
    assert by_rule["limited-rule"] == [("failed", 0), ("failed", 0)]  # попытка не засчитана
    assert by_rule["other-rule"] == [("failed", 1), ("failed", 1)]
    again = await store.claim_notifications(limit=10, claim_for=CLAIM)
    assert {item.channel_id for item in again} == {other}  # канал на паузе не захватывается


async def test_claim_limit_applies_after_disabled_channels_are_filtered(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    muted = await _channel(space, "muted")
    await _rule(space, muted, "muted-rule")
    await hub_under_test._notification_dispatcher.handle_events(await _events(hub_under_test, space, 2))
    live = await _channel(space, "live")
    await _rule(space, live, "live-rule")
    await space.notifications.update_telegram_channel(muted, enabled=False)
    events = await _events(hub_under_test, space, 1)
    await hub_under_test._notification_dispatcher.handle_events(events[-1:])

    claimed = await hub_under_test._store.claim_notifications(limit=1, claim_for=CLAIM)

    assert [item.channel_id for item in claimed] == [live]


async def test_rules_are_listed_in_the_same_order(space: SpaceHandle) -> None:
    """R2-P13: порядок `rules()` не зависит от хранилища и от последнего изменения."""
    channel_id = await _channel(space)
    ids = {name: await _rule(space, channel_id, name) for name in ("charlie", "alpha", "bravo")}

    await space.notifications.update_rule(ids["alpha"], enabled=False)

    assert [rule.name for rule in await space.notifications.rules()] == ["alpha", "bravo", "charlie"]


async def test_rule_for_missing_channel_is_not_found(space: SpaceHandle, hub_under_test: IdentityHub) -> None:
    """R2-P13: несуществующий или чужой канал — `NotFound`, а не `KeyError`."""
    with pytest.raises(NotFound):
        await _rule(space, uuid4())
    foreign = await _channel(hub_under_test.space(f"other-{uuid4().hex[:6]}", create=True))
    with pytest.raises(NotFound):
        await _rule(space, foreign)
