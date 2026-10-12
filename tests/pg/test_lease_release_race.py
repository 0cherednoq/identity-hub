"""IMP-06 (I2.4): гонка release и revoke — исход детерминирован порядком коммитов (D-213)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub import IdentityCreate, LeaseRevoked
from identity_hub.hub import SpaceHandle
from identity_hub.store.base import LeaseCounters

pytestmark = [pytest.mark.pg]


async def _lease(space: SpaceHandle) -> Any:
    await space.identities.create(
        IdentityCreate(service="fake_tg", external_ref="+race", lifecycle="ready", session={"auth_key": "k"}),
        actor="test",
    )
    return await space.pools.define("work", service="fake_tg", holder="w").acquire()


async def _wait_blocked(sessionmaker: async_sessionmaker[AsyncSession], task: asyncio.Task[object]) -> None:
    blocked = text("SELECT count(*) FROM pg_locks WHERE NOT granted")
    async with sessionmaker() as session:
        for _ in range(500):
            assert not task.done(), "конкурент не дождался незакоммиченной транзакции"
            if (await session.execute(blocked)).scalar_one():
                return
            await asyncio.sleep(0.01)
    pytest.fail("конкурент не встал на блокировку")


async def test_revoke_committed_first_makes_release_raise(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    lease: Any = await _lease(space)
    held, go = asyncio.Event(), asyncio.Event()

    async def revoker() -> None:
        async with space._tx(own=True) as tx:
            assert await tx.revoke_lease(lease.identity_id) is not None
            held.set()
            await go.wait()

    first = asyncio.create_task(revoker())
    await held.wait()
    release = asyncio.create_task(lease.release())
    await _wait_blocked(sessionmaker, release)
    go.set()
    await first

    with pytest.raises(LeaseRevoked):
        await release


async def test_release_committed_first_makes_revoke_noop(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    lease: Any = await _lease(space)
    held, go = asyncio.Event(), asyncio.Event()

    async def releaser() -> None:
        async with space._tx(own=True) as tx:
            outcome = await tx.release_lease(
                lease._row,
                "released",
                LeaseCounters.empty(),
                shared=False,
            )
            assert outcome == "released"
            held.set()
            await go.wait()

    first = asyncio.create_task(releaser())
    await held.wait()
    revoke = asyncio.create_task(space.pools.revoke(lease.identity_id, reason="тест", actor="admin"))
    await _wait_blocked(sessionmaker, revoke)
    go.set()
    await first

    assert await revoke is False  # отзывать уже нечего
