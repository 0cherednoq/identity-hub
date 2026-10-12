"""C4.2: `start="now"` на PostgreSQL — существующий курсор и транзакции, открытые при создании."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub.events import Event, EventDraft
from identity_hub.hub import IdentityHub, SpaceHandle

pytestmark = pytest.mark.pg


async def _emit(space: SpaceHandle, n: int) -> None:
    async with space._tx(own=True) as tx:
        await space._emit(tx, EventDraft("identity.updated", actor="test", data={"n": n}))


@pytest.mark.parametrize("attempt", range(3))
async def test_open_transaction_is_not_lost(
    hub: IdentityHub, space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession], attempt: int
) -> None:
    """C4.2: событие транзакции, открытой при создании курсора, приходит после её фиксации."""
    seen: list[int] = []

    async def callback(event: Event) -> None:
        seen.append(event.data["n"])

    await _emit(space, 1)
    async with sessionmaker() as user:
        await user.begin()
        async with space.using(user)._tx() as tx:
            await space._emit(tx, EventDraft("identity.updated", actor="test", data={"n": 2}))
        hub.events.subscribe(f"late-{attempt}", callback, types={"identity.updated"}, start="now")
        await hub.dispatcher.run_once()  # курсор создан, пока транзакция открыта
        assert seen == []
        await user.commit()
    await _emit(space, 3)

    await hub.dispatcher.drain()

    assert seen == [2, 3]


async def test_existing_cursor_is_not_moved(hub: IdentityHub, space: SpaceHandle) -> None:
    """C4.2: у подписчика с курсором параметр ничего не меняет — пропущенное за простой приходит."""
    seen: list[int] = []

    async def callback(event: Event) -> None:
        seen.append(event.data["n"])

    async def nothing(events: list[Event]) -> int | None:
        return None

    await hub._store.process_events("callbacks:steady", nothing, limit=10)  # курсор уже есть
    await _emit(space, 1)
    hub.events.subscribe("steady", callback, types={"identity.updated"}, start="now")

    await hub.dispatcher.drain()

    assert seen == [1]
