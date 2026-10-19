"""F3.1: сбойное событие не останавливает подписчика и остальной диспетчер (R1-06)."""

from __future__ import annotations

from typing import Any

import pytest

from identity_hub import IdentityHub, SpaceHandle, ValidationFailed
from identity_hub.events import Event, EventDraft
from identity_hub.read.events import EventFilters

pytestmark = pytest.mark.conformance


async def _emit_numbered(space: SpaceHandle, count: int) -> None:
    for n in range(1, count + 1):
        async with space._tx(own=True) as tx:
            await space._emit(tx, EventDraft("identity.updated", actor="test", data={"n": n}))


async def _skipped(space: SpaceHandle) -> list[Any]:
    page = await space.admin.event_page(
        filters=EventFilters(type="dispatcher.event.skipped", include_journal=True)
    )
    return list(page.items)


async def test_poison_event_is_skipped_after_attempt_limit(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    """Пять событий, колбэк падает на третьем: 1 и 2 не повторяются, 4 и 5 доставлены, 3 пропущено."""
    seen: list[int] = []

    async def callback(event: Event) -> None:
        seen.append(event.data["n"])
        if event.data["n"] == 3:
            raise RuntimeError("poison secret-text")

    hub_under_test.events.subscribe("audit", callback, types={"identity.updated"})
    await _emit_numbered(space, 5)

    await hub_under_test.dispatcher.run_once()
    assert seen == [1, 2, 3]  # курсор остановился перед сбойным событием
    assert hub_under_test.dispatcher.runtime["callbacks:audit"].retrying is True
    assert await _skipped(space) == []

    for _ in range(6):
        await hub_under_test.dispatcher.run_once()

    assert seen == [1, 2, 3, 3, 3, 3, 3, 4, 5]  # лимит по умолчанию — 5 попыток
    assert hub_under_test.dispatcher.runtime["callbacks:audit"].retrying is False
    (skipped,) = await _skipped(space)
    assert skipped.actor == "dispatcher" and skipped.external is False
    assert skipped.data["consumer"] == "callbacks:audit"
    assert skipped.data["type"] == "identity.updated"
    assert (skipped.data["attempts"], skipped.data["error"]) == (5, "RuntimeError")
    assert "secret-text" not in str(dict(skipped.data))

    await _emit_numbered(space, 1)  # подписчик продолжает работать
    await hub_under_test.dispatcher.run_once()
    assert seen[-1] == 1


async def test_max_attempts_is_configurable_and_counted_per_event(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    attempts: dict[int, int] = {}

    async def callback(event: Event) -> None:
        n = event.data["n"]
        attempts[n] = attempts.get(n, 0) + 1
        if n in (2, 4):
            raise ValueError("bad")

    hub_under_test.events.subscribe("strict", callback, types={"identity.updated"}, max_attempts=2)
    await _emit_numbered(space, 5)

    await hub_under_test.dispatcher.drain()
    for _ in range(4):
        await hub_under_test.dispatcher.run_once()

    assert attempts == {1: 1, 2: 2, 3: 1, 4: 2, 5: 1}
    assert sorted(item.data["attempts"] for item in await _skipped(space)) == [2, 2]


async def test_block_policy_keeps_cursor_at_poison_event_until_success(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    seen: list[int] = []
    failing = True

    async def callback(event: Event) -> None:
        nonlocal failing
        seen.append(event.data["n"])
        if event.data["n"] == 2 and failing:
            raise RuntimeError("temporary")

    hub_under_test.events.subscribe(
        "projection", callback, types={"identity.updated"}, failure_policy="block"
    )
    await _emit_numbered(space, 3)

    for _ in range(6):
        await hub_under_test.dispatcher.run_once()

    assert seen == [1, 2, 2, 2, 2, 2, 2]
    runtime = hub_under_test.dispatcher.runtime["callbacks:projection"]
    assert runtime.retrying is True and runtime.stalled is True and runtime.failure_attempts == 6
    process = next(
        item
        for item in (await hub_under_test.background_snapshot()).processes
        if item.name == "callbacks:projection"
    )
    assert process.stalled is True and process.failure_attempts == 6 and process.lag_events == 2
    assert await _skipped(space) == []

    failing = False
    await hub_under_test.dispatcher.run_once()

    assert seen[-2:] == [2, 3]
    runtime = hub_under_test.dispatcher.runtime["callbacks:projection"]
    assert runtime.retrying is False and runtime.stalled is False and runtime.failure_attempts == 0
    process = next(
        item
        for item in (await hub_under_test.background_snapshot()).processes
        if item.name == "callbacks:projection"
    )
    assert process.stalled is False and process.failure_attempts == 0 and process.lag_events == 0


def test_block_policy_rejects_attempt_limit_and_unknown_value(hub_under_test: IdentityHub) -> None:
    async def callback(event: Event) -> None:
        return None

    with pytest.raises(ValidationFailed, match="max_attempts"):
        hub_under_test.events.subscribe("projection", callback, failure_policy="block", max_attempts=2)
    with pytest.raises(ValidationFailed, match="failure_policy"):
        hub_under_test.events.subscribe("projection", callback, failure_policy="unknown")  # type: ignore[arg-type]


async def test_skip_notice_is_not_delivered_back_to_subscribers(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    """Запись о пропуске — журнальная: подписчик, падающий на всём, не порождает бесконечную цепочку."""
    calls = 0

    async def always_fails(event: Event) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("down")

    hub_under_test.events.subscribe("broken", always_fails, max_attempts=1)
    await _emit_numbered(space, 3)

    for _ in range(10):
        await hub_under_test.dispatcher.run_once()

    assert calls == 3
    assert len(await _skipped(space)) == 3


async def test_failing_batch_consumer_does_not_block_others(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    """Сбой пакетного потребителя или pump-а не останавливает остальных потребителей и pump-ы."""
    delivered: list[int] = []
    pumped: list[str] = []

    async def broken_batch(events: list[Event]) -> None:
        raise RuntimeError("batch consumer is down")

    async def healthy(event: Event) -> None:
        delivered.append(event.data["n"])

    async def broken_pump() -> int:
        raise RuntimeError("pump is down")

    async def healthy_pump() -> int:
        pumped.append("ok")
        return 0

    dispatcher = hub_under_test.dispatcher
    dispatcher._consumers = {}  # порядок важен: сбойный потребитель идёт первым
    dispatcher._pumps = []
    dispatcher.add_consumer("broken", broken_batch)
    hub_under_test.events.subscribe("healthy", healthy, types={"identity.updated"})
    dispatcher.add_pump(broken_pump)
    dispatcher.add_pump(healthy_pump)
    await _emit_numbered(space, 3)

    await dispatcher.run_once()
    await dispatcher.run_once()

    assert delivered == [1, 2, 3]
    assert pumped == ["ok", "ok"]
    assert dispatcher.runtime["broken"].retrying is True
    assert dispatcher.runtime["callbacks:healthy"].retrying is False
