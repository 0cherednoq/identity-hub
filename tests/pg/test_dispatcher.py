"""P8.1: диспетчер событий (ТЗ FR-13.5, 13.6, G-5.2–5.5)."""

from __future__ import annotations

import asyncio

import pytest

from identity_hub.dispatch.dispatcher import Dispatcher
from identity_hub.events import Event, EventDraft
from identity_hub.hub import IdentityHub, SpaceHandle

pytestmark = pytest.mark.pg


async def _emit(space: SpaceHandle, event_type: str = "pool.capacity.low", **data: object) -> None:
    async with space._tx() as tx:
        await space._emit(tx, EventDraft(event_type, actor="t", data=data))


async def test_callbacks_filtered_and_at_least_once(hub: IdentityHub, space: SpaceHandle) -> None:
    got: list[Event] = []
    audit: list[str] = []
    failures = {"left": 1}

    async def on_event(event: Event) -> None:
        got.append(event)

    async def flaky(event: Event) -> None:
        if failures["left"]:
            failures["left"] -= 1
            raise RuntimeError("временная ошибка")
        audit.append(event.type)

    hub.events.subscribe("all", on_event)
    hub.events.subscribe("audit", flaky, types={"identity.state.*"})
    await space.ref()
    await _emit(space, n=1)
    async with space._tx() as tx:
        await space._emit(tx, EventDraft("lease.acquired", actor="t"))  # только журнал — не доставляется
    await _emit(space, "identity.state.drift", n=2)

    await hub.dispatcher.run_once()
    assert [e.type for e in got] == ["pool.capacity.low", "identity.state.drift"]
    assert got[0].space == "tennet" and got[0].data == {"n": 1}
    assert audit == []  # пачка упала — курсор не сдвинут
    await hub.dispatcher.run_once()
    assert audit == ["identity.state.drift"]  # повтор после ошибки (at-least-once)
    assert len(got) == 2  # у другого потребителя свой курсор — без повторов


async def test_late_commit_of_earlier_transaction_not_skipped(hub: IdentityHub, space: SpaceHandle) -> None:
    """G-5.2: событие ранней транзакции, закоммиченной позже, курсор не пропускает."""
    got: list[int] = []

    async def on_event(event: Event) -> None:
        got.append(int(event.data["n"]))

    hub.events.subscribe("seq", on_event)
    await space.ref()
    early_written = asyncio.Event()
    release_early = asyncio.Event()

    async def early() -> None:
        async with space._tx() as tx:
            await space._emit(tx, EventDraft("pool.capacity.low", actor="t", data={"n": 1}))
            early_written.set()
            await release_early.wait()

    task = asyncio.create_task(early())
    await early_written.wait()
    await _emit(space, n=2)  # поздняя транзакция коммитится первой
    await hub.dispatcher.drain()
    assert got == []  # событие 2 выше горизонта, пока жива ранняя транзакция
    release_early.set()
    await task
    await hub.dispatcher.drain()
    assert sorted(got) == [1, 2]


async def test_two_dispatchers_do_not_duplicate(hub: IdentityHub, space: SpaceHandle) -> None:
    got: list[int] = []
    gate = asyncio.Event()

    async def slow(events: list[Event]) -> None:
        await gate.wait()
        got.extend(int(e.data["n"]) for e in events)

    first, second = Dispatcher(hub), Dispatcher(hub)
    first.add_consumer("webhooks-test", slow)
    second.add_consumer("webhooks-test", slow)
    await space.ref()
    for n in range(3):
        await _emit(space, n=n)
    running = asyncio.create_task(first.run_once())
    await asyncio.sleep(0.3)
    assert await second.run_once() == 0  # курсор занят первым экземпляром (SKIP LOCKED)
    gate.set()
    assert await running == 3
    assert await second.run_once() == 0
    assert got == [0, 1, 2]


async def test_listen_wakes_dispatcher(hub: IdentityHub, space: SpaceHandle) -> None:
    got = asyncio.Event()

    async def on_event(event: Event) -> None:
        got.set()

    hub.dispatcher.poll_interval = 30  # без NOTIFY событие ждало бы 30 с
    hub.events.subscribe("wake", on_event)
    await space.ref()
    async with hub.dispatcher.running():
        await asyncio.sleep(0.3)
        await _emit(space, n=1)
        await asyncio.wait_for(got.wait(), timeout=5)
