"""N1.3b (задача 5): исчерпанные аккаунты уходят в хвост порядка выборки, `last_leased_at` не трогается."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from tests.fixtures.services import FAKE_TG

from identity_hub import IdentityCreate, IdentityHub, QuotaExhausted, SpaceHandle
from identity_hub.clock import FakeClock
from identity_hub.service import OperationRule, Quota
from identity_hub.store.base import PICK_DEMOTE_CHUNK

pytestmark = [pytest.mark.anyio, pytest.mark.conformance]

QUOTA_TG = replace(
    FAKE_TG,
    name="order_tg",
    operations={"send": OperationRule(quotas=(Quota(1, timedelta(hours=1)),)), "read": OperationRule()},
)


async def _space(hub: IdentityHub, space: SpaceHandle, count: int) -> list[UUID]:
    hub.register_service(QUOTA_TG)
    await hub.start()  # строка сервиса в БД
    specs = [
        IdentityCreate(
            service="order_tg", external_ref=f"+{n:03d}", lifecycle="ready", session={"auth_key": "k"}
        )
        for n in range(count)
    ]
    ids = []
    for offset in range(0, count, 1000):
        created = await space.identities.create_many(specs[offset : offset + 1000], actor="test")
        ids.extend(item.id for item in created.created)
    return ids


async def _tick(hub: IdentityHub) -> None:
    """Сдвинуть время, чтобы соседние отметки порядка различались на обоих бэкендах."""
    if isinstance(hub.clock, FakeClock):
        hub.clock.advance(timedelta(seconds=1))
    else:
        await asyncio.sleep(0.02)


async def _exhaust(hub: IdentityHub, pool: Any, identity_id: UUID) -> None:
    lease = await pool.acquire(among=[identity_id])
    async with lease.operation("send"):
        pass
    with pytest.raises(QuotaExhausted):
        async with lease.operation("send"):
            pass
    await lease.release()
    await _tick(hub)


async def _row(space: SpaceHandle, identity_id: UUID) -> Any:
    async with space._tx(own=True) as tx:
        return await tx.get_identity(identity_id)


async def test_acquire_sets_the_pick_order(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    (only,) = await _space(hub_under_test, space, 1)
    assert (await _row(space, only)).pick_order_at is None

    lease = await space.pools.define("work", service="order_tg", holder="w").acquire()

    row = await _row(space, only)
    assert row.pick_order_at is not None and row.pick_order_at == row.last_leased_at
    await lease.release()


async def test_exhausted_head_moves_to_the_tail(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    first, second, free = await _space(hub_under_test, space, 3)
    pool = space.pools.define("work", service="order_tg", holder="w")
    await _exhaust(hub_under_test, pool, first)
    await _exhaust(hub_under_test, pool, second)
    warm = await pool.acquire(among=[free])  # свободный арендован позже всех: исчерпанные впереди
    await warm.release()
    await _tick(hub_under_test)
    before = {identity_id: await _row(space, identity_id) for identity_id in (first, second, free)}

    lease = await pool.acquire(for_operation="send", require_quota=True)

    assert lease.identity_id == free
    for identity_id in (first, second):
        row = await _row(space, identity_id)
        assert row.pick_order_at > before[identity_id].pick_order_at  # перенесён в хвост
        assert row.last_leased_at == before[identity_id].last_leased_at  # время аренды прежнее
    await lease.release()


async def test_plain_pick_moves_nothing(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    exhausted, _other = await _space(hub_under_test, space, 2)
    pool = space.pools.define("work", service="order_tg", holder="w")
    await _exhaust(hub_under_test, pool, exhausted)
    before = await _row(space, exhausted)

    for options in ({}, {"for_operation": "send"}):  # без require_quota порядок никто не трогает
        lease = await pool.acquire(exclude=[exhausted], **options)
        await lease.release()

    assert (await _row(space, exhausted)).pick_order_at == before.pick_order_at


async def test_lru_without_quotas_follows_the_pick_order(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    ids = await _space(hub_under_test, space, 3)
    pool = space.pools.define("work", service="order_tg", holder="w")
    seen = []
    for _ in range(6):
        lease = await pool.acquire()
        seen.append(lease.identity_id)
        await lease.release()
        await _tick(hub_under_test)

    assert seen[:3] == seen[3:] and set(seen[:3]) == set(ids)  # ровный круг, как до N1.3b


async def test_long_exhausted_head_is_cleared_in_bounded_chunks(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    count = PICK_DEMOTE_CHUNK + 6
    ids = await _space(hub_under_test, space, count + 1)
    free = ids[-1]
    async with space._tx(own=True) as tx:  # исчерпать напрямую: десятки аренд тест не ускоряют
        for identity_id in ids[:count]:
            await _exhaust_directly(tx, identity_id)

    pool = space.pools.define("work", service="order_tg", holder="w")
    lease = await pool.acquire(for_operation="send", require_quota=True)

    assert lease.identity_id == free
    moved = [identity_id for identity_id in ids[:count] if (await _row(space, identity_id)).pick_order_at]
    assert len(moved) == PICK_DEMOTE_CHUNK  # один захват не делает неограниченную работу
    await lease.release()

    lease = await pool.acquire(for_operation="send", require_quota=True)
    moved = [identity_id for identity_id in ids[:count] if (await _row(space, identity_id)).pick_order_at]
    assert len(moved) == count  # следующая ограниченная пачка продолжает очистку головы
    await lease.release()


async def _exhaust_directly(tx: Any, identity_id: UUID) -> None:
    from identity_hub.store.base import QuotaCharge

    quota = QUOTA_TG.rule("send").quotas[0]
    outcome = await tx.charge_quotas(
        identity_id, [QuotaCharge("send", quota.window_seconds, quota.origin, quota.limit)]
    )
    assert outcome.exhausted is None
