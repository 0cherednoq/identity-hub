"""C0.2: пакетный контракт на PostgreSQL — порядок блокировок и транзакция приложения."""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub.errors import ValidationFailed
from identity_hub.hub import SpaceHandle
from identity_hub.postgres.models import EventRecord

pytestmark = pytest.mark.pg


async def _proxies(space: SpaceHandle, count: int) -> list[UUID]:
    return [(await space.proxies.create(f"http://10.8.0.{n}:80", group="res")).id for n in range(count)]


def _mover(group: str, bad: UUID | None = None):  # type: ignore[no-untyped-def]
    async def move(handle: SpaceHandle, proxy_id: UUID) -> None:
        await handle.proxies.move(proxy_id, group=group, actor="test")
        await asyncio.sleep(0)  # уступить соседней пачке между блокировками строк
        if proxy_id == bad:
            raise ValidationFailed("нельзя")

    return move


@pytest.mark.parametrize("attempt", range(3))
async def test_overlapping_batches_do_not_deadlock(space: SpaceHandle, attempt: int) -> None:
    """C0.2: две пачки с пересекающимися ключами в разном порядке входа завершаются обе."""
    ids = await _proxies(space, 12)

    first, second = await asyncio.wait_for(
        asyncio.gather(
            space._run_batch(ids, _mover("a")),
            space._run_batch(list(reversed(ids)), _mover("b")),
        ),
        timeout=30,
    )

    assert first.applied == sorted(ids) == second.applied
    groups = {(await space.proxies.get(proxy_id)).group for proxy_id in ids}
    assert groups in ({"a"}, {"b"})  # пачки прошли одна за другой, а не вперемешку


async def test_batch_joins_user_transaction(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """C0.2: пачка под `using(session)` идёт в транзакции приложения и откатывается вместе с ней."""
    ids = await _proxies(space, 3)
    async with sessionmaker() as session:
        before = (await session.execute(select(func.count()).select_from(EventRecord))).scalar_one()

    async with sessionmaker() as user:
        await user.begin()
        result = await space.using(user)._run_batch(ids, _mover("dc", bad=ids[1]))
        assert sorted(result.applied) == sorted([ids[0], ids[2]])
        assert [r.key for r in result.rejected] == [ids[1]]
        await user.rollback()

    assert {(await space.proxies.get(proxy_id)).group for proxy_id in ids} == {"res"}
    async with sessionmaker() as session:
        assert (await session.execute(select(func.count()).select_from(EventRecord))).scalar_one() == before

    async with sessionmaker() as user, user.begin():
        await space.using(user)._run_batch(ids, _mover("dc", bad=ids[1]))
    assert [(await space.proxies.get(proxy_id)).group for proxy_id in ids] == ["dc", "res", "dc"]


async def test_identity_batch_joins_user_transaction(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """C2.1: пакетные команды аккаунтов под `using(session)` откатываются вместе с приложением."""
    from identity_hub import IdentityCreate, Lifecycle

    ids = [
        (await space.identities.create(IdentityCreate(service="fake_tg", external_ref=f"+{n}"), actor="t")).id
        for n in range(3)
    ]
    async with sessionmaker() as session:
        before = (await session.execute(select(func.count()).select_from(EventRecord))).scalar_one()

    async with sessionmaker() as user:
        await user.begin()
        identities = space.using(user).identities
        activated = await identities.activate_many(ids[:2], actor="op")
        deleted = await identities.delete_many(ids[2:], actor="op")
        assert sorted(activated.applied) == sorted(ids[:2]) and deleted.applied == ids[2:]
        await user.rollback()

    assert {(await space.identities.get(i)).lifecycle for i in ids} == {Lifecycle.PROVISIONING}
    async with sessionmaker() as session:
        assert (await session.execute(select(func.count()).select_from(EventRecord))).scalar_one() == before

    async with sessionmaker() as user, user.begin():
        await space.using(user).identities.activate_many(ids, actor="op")
    assert {(await space.identities.get(i)).lifecycle for i in ids} == {Lifecycle.READY}
