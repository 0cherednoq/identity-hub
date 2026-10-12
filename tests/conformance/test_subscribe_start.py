"""C4.2: новый подписчик может начать с текущего момента, а не с начала журнала."""

from __future__ import annotations

import pytest

from identity_hub import IdentityHub, SpaceHandle
from identity_hub.events import Event, EventDraft

pytestmark = pytest.mark.conformance


async def _emit(space: SpaceHandle, numbers: list[int]) -> None:
    for n in numbers:
        async with space._tx(own=True) as tx:
            await space._emit(tx, EventDraft("identity.updated", actor="test", data={"n": n}))


async def test_start_now_skips_stored_history(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    """C4.2: подписчик `start="now"` не получает историю и получает всё, что записано после."""
    fresh: list[int] = []
    batch: list[int] = []
    full: list[int] = []

    async def on_fresh(event: Event) -> None:
        fresh.append(event.data["n"])

    async def on_batch(events: list[Event]) -> None:
        batch.extend(event.data["n"] for event in events)

    async def on_full(event: Event) -> None:
        full.append(event.data["n"])

    await _emit(space, [1, 2, 3])
    hub_under_test.events.subscribe("fresh", on_fresh, types={"identity.updated"}, start="now")
    hub_under_test.events.subscribe_batch("batch", on_batch, types={"identity.updated"}, start="now")
    hub_under_test.events.subscribe("full", on_full, types={"identity.updated"})

    await hub_under_test.dispatcher.drain()
    assert (fresh, batch, full) == ([], [], [1, 2, 3])

    await _emit(space, [4, 5])
    await hub_under_test.dispatcher.drain()
    assert (fresh, batch, full) == ([4, 5], [4, 5], [1, 2, 3, 4, 5])


async def test_start_is_validated(hub_under_test: IdentityHub) -> None:
    """C4.2: допустимы только `beginning` и `now`."""

    async def callback(event: Event) -> None:
        return None

    with pytest.raises(ValueError):
        hub_under_test.events.subscribe("bad", callback, start="yesterday")
