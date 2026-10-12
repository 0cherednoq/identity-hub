"""C1.5: пакетная перепривязка на PostgreSQL — ёмкость под конкуренцией и транзакция приложения."""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub.hub import SpaceHandle
from identity_hub.postgres.models import BindingRecord, EventRecord
from tests.fixtures.api import register

pytestmark = pytest.mark.pg


async def _accounts(space: SpaceHandle, count: int) -> list[UUID]:
    return [
        (await register(space, service="fake_tg", external_ref=f"+{n}")).identity_id for n in range(count)
    ]


async def _count(sessionmaker: async_sessionmaker[AsyncSession], model: type) -> int:
    async with sessionmaker() as session:
        return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


@pytest.mark.parametrize("attempt", range(3))
async def test_parallel_batches_respect_capacity(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession], attempt: int
) -> None:
    """C1.5: четыре параллельные пачки на прокси с ёмкостью 5 привязывают ровно пять аккаунтов."""
    proxy = await space.proxies.create("http://10.15.0.1:80", group="g", max_identities=5)
    accounts = await _accounts(space, 12)
    batches = [accounts[n::4] for n in range(4)]

    results = await asyncio.wait_for(
        asyncio.gather(
            *(
                space.proxies.rebind_many(dict.fromkeys(batch, proxy.id), reason="тест", actor="op")
                for batch in batches
            )
        ),
        timeout=60,
    )

    applied = [identity_id for result in results for identity_id in result.applied]
    rejected = [r for result in results for r in result.rejected]
    assert len(applied) == 5 and len(rejected) == 7
    assert {r.code for r in rejected} == {"no_resource_available"}
    assert await _count(sessionmaker, BindingRecord) == 5
    listed = (await space.proxies.list()).items
    assert [p.bound_count for p in listed] == [5]


async def test_unbind_and_rebind_many_in_user_transaction(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """C1.5: перепривязка и отвязка под `using(session)` откатываются вместе с приложением."""
    first = await space.proxies.create("http://10.15.1.1:80", group="g")
    second = await space.proxies.create("http://10.15.1.2:80", group="g")
    one, two = await _accounts(space, 2)
    await space.proxies.rebind(one, first.id, reason="тест", actor="op")
    events = await _count(sessionmaker, EventRecord)

    async with sessionmaker() as user:
        await user.begin()
        proxies = space.using(user).proxies
        moved = await proxies.rebind_many({one: second.id, two: second.id}, reason="тест", actor="op")
        assert sorted(moved.applied) == sorted([one, two])
        assert await proxies.unbind(two, reason="тест", actor="op") is True
        await user.rollback()

    assert await _count(sessionmaker, EventRecord) == events
    bound = {p.id: p.bound_count for p in (await space.proxies.list()).items}
    assert bound == {first.id: 1, second.id: 0}

    async with sessionmaker() as user, user.begin():
        await space.using(user).proxies.rebind_many({one: second.id}, reason="тест", actor="op")
    bound = {p.id: p.bound_count for p in (await space.proxies.list()).items}
    assert bound == {first.id: 0, second.id: 1}
