"""C4.1: пакетная подписка на события с защитой от «ядовитого» события и фильтрами."""

from __future__ import annotations

from typing import Any

import pytest

from identity_hub import IdentityHub, SpaceHandle, ValidationFailed
from identity_hub.events import Event, EventDraft
from identity_hub.read.events import EventFilters

pytestmark = pytest.mark.conformance


async def _emit(
    space: SpaceHandle, numbers: list[int], *, type_: str = "identity.updated", **fields: Any
) -> None:
    for n in numbers:
        async with space._tx(own=True) as tx:
            await space._emit(tx, EventDraft(type_, actor="test", data={"n": n}, **fields))


async def _skipped(space: SpaceHandle) -> list[Any]:
    page = await space.admin.event_page(
        filters=EventFilters(type="dispatcher.event.skipped", include_journal=True)
    )
    return list(page.items)


async def test_handler_receives_events_in_batches(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    """C4.1: обработчик получает пачку целиком и в порядке журнала."""
    batches: list[list[int]] = []

    async def handler(events: list[Event]) -> None:
        batches.append([event.data["n"] for event in events])

    hub_under_test.events.subscribe_batch("mirror", handler, types={"identity.updated"})
    await _emit(space, [1, 2, 3, 4, 5])
    await _emit(space, [99], type_="proxy.updated")

    await hub_under_test.dispatcher.drain()

    assert batches == [[1, 2, 3, 4, 5]]
    assert "callbacks:mirror" in hub_under_test.dispatcher.consumers
    await _emit(space, [6])
    await hub_under_test.dispatcher.drain()
    assert batches[-1] == [6]


async def test_poison_event_is_isolated_and_skipped(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    """C4.1: одно сбойное событие из восьми не останавливает подписчика; остальные доставлены."""
    delivered: list[int] = []
    calls: list[list[int]] = []

    async def handler(events: list[Event]) -> None:
        numbers = [event.data["n"] for event in events]
        calls.append(numbers)
        if 5 in numbers:
            raise RuntimeError("poison secret-text")
        delivered.extend(numbers)

    hub_under_test.events.subscribe_batch("mirror", handler, types={"identity.updated"}, max_attempts=3)
    await _emit(space, list(range(1, 9)))

    await hub_under_test.dispatcher.run_once()

    assert delivered == [1, 2, 3, 4]  # события до сбойного доставлены и больше не повторяются
    assert hub_under_test.dispatcher.runtime["callbacks:mirror"].retrying is True
    assert await _skipped(space) == []

    for _ in range(4):
        await hub_under_test.dispatcher.run_once()

    assert sorted(delivered) == [1, 2, 3, 4, 6, 7, 8] and delivered.count(1) == 1
    assert sum(numbers == [5] for numbers in calls) == 3  # ровно max_attempts попыток в одиночку
    assert hub_under_test.dispatcher.runtime["callbacks:mirror"].retrying is False
    (skipped,) = await _skipped(space)
    assert skipped.data["consumer"] == "callbacks:mirror"
    assert (skipped.data["attempts"], skipped.data["error"]) == (3, "RuntimeError")
    assert "secret-text" not in str(dict(skipped.data))


async def test_block_policy_delivers_prefix_and_retries_poison_event(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    delivered: list[int] = []
    failing = True

    async def handler(events: list[Event]) -> None:
        numbers = [event.data["n"] for event in events]
        if 3 in numbers and failing:
            raise RuntimeError("temporary")
        delivered.extend(numbers)

    hub_under_test.events.subscribe_batch(
        "projection", handler, types={"identity.updated"}, failure_policy="block"
    )
    await _emit(space, [1, 2, 3, 4])

    for _ in range(4):
        await hub_under_test.dispatcher.run_once()

    assert delivered == [1, 2]
    assert hub_under_test.dispatcher.runtime["callbacks:projection"].retrying is True
    assert await _skipped(space) == []

    failing = False
    await hub_under_test.dispatcher.run_once()

    assert delivered == [1, 2, 3, 4]
    assert hub_under_test.dispatcher.runtime["callbacks:projection"].retrying is False


async def test_filters_by_space_and_service(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    """C4.1: фильтры `space` и `service` — у пакетной и у обычной подписки."""
    other = hub_under_test.space(f"{space.slug}-other", create=True)
    by_space: list[tuple[str, int]] = []
    by_service: list[int] = []
    single: list[int] = []

    async def space_handler(events: list[Event]) -> None:
        by_space.extend((event.space, event.data["n"]) for event in events)

    async def service_handler(events: list[Event]) -> None:
        by_service.extend(event.data["n"] for event in events)

    async def callback(event: Event) -> None:
        single.append(event.data["n"])

    hub_under_test.events.subscribe_batch("by-space", space_handler, space=space)
    hub_under_test.events.subscribe_batch("by-service", service_handler, space=space.slug, service="fake_tg")
    hub_under_test.events.subscribe("single", callback, space=other, service="fake_exchange")
    await _emit(space, [1], service="fake_tg")
    await _emit(space, [2], service="fake_exchange")
    await _emit(space, [3])
    await _emit(other, [4], service="fake_tg")
    await _emit(other, [5], service="fake_exchange")

    await hub_under_test.dispatcher.drain()

    assert by_space == [(space.slug, 1), (space.slug, 2), (space.slug, 3)]
    assert by_service == [1] and single == [5]


async def test_subscribe_batch_validates_arguments(hub_under_test: IdentityHub) -> None:
    """C4.1: `max_attempts` положителен; имя подписчика уникально среди всех колбэков."""

    async def handler(events: list[Event]) -> None:
        return None

    with pytest.raises(ValueError):
        hub_under_test.events.subscribe_batch("mirror", handler, max_attempts=0)
    with pytest.raises(ValidationFailed, match="max_attempts"):
        hub_under_test.events.subscribe_batch("mirror", handler, failure_policy="block", max_attempts=2)
    hub_under_test.events.subscribe_batch("mirror", handler)
    with pytest.raises(ValueError):
        hub_under_test.events.subscribe_batch("mirror", handler)
