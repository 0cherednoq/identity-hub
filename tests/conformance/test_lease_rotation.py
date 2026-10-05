"""IMP-01 (I2.1): аренда выдаёт наименее недавно арендованный аккаунт (D-209)."""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest
from tests.fixtures.services import FAKE_TG

from identity_hub import IdentityCreate, IdentityHub, LeaseMode, SpaceHandle

pytestmark = [pytest.mark.anyio, pytest.mark.conformance]


async def _ready(space: SpaceHandle, count: int) -> list[UUID]:
    result = await space.identities.create_many(
        [
            IdentityCreate(
                service="fake_tg", external_ref=f"+{n}", lifecycle="ready", session={"auth_key": "k"}
            )
            for n in range(count)
        ],
        actor="test",
    )
    return [identity.id for identity in result.created]


async def _last_leased(space: SpaceHandle, identity_id: UUID) -> object:
    async with space._tx(own=True) as tx:
        row = await tx.get_identity(identity_id)
    assert row is not None
    return row.last_leased_at


async def test_rotation_is_even(space: SpaceHandle) -> None:
    ids = await _ready(space, 4)
    pool = space.pools.define("work", service="fake_tg", holder="w")

    order = []
    for _ in range(8):
        lease = await pool.acquire()
        order.append(lease.identity_id)
        await lease.release()

    assert sorted(order[:4]) == sorted(ids)  # первый круг — каждый по разу
    assert order[4:] == order[:4]  # второй круг — в том же порядке давности


async def test_exclusive_and_shared_leases_stamp_last_leased_at(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    first = min(await _ready(space, 2))  # без истории аренд — по id
    assert await _last_leased(space, first) is None
    lease = await space.pools.define("work", service="fake_tg", holder="w").acquire()
    assert lease.identity_id == first and await _last_leased(space, first) is not None
    await lease.release()

    hub_under_test.register_service(replace(FAKE_TG, name="fake_shared", lease_mode=LeaseMode.SHARED))
    await hub_under_test.start()  # строка сервиса в БД
    created = await space.identities.create(
        IdentityCreate(
            service="fake_shared", external_ref="+s", lifecycle="ready", session={"auth_key": "k"}
        ),
        actor="test",
    )
    shared = await space.pools.define("read", service="fake_shared", holder="r").acquire()
    assert shared.identity_id == created.id and await _last_leased(space, created.id) is not None
    await shared.release()
