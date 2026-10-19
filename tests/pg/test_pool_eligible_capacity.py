"""PostgreSQL-специфичный контракт SQL-подзапроса для полной ёмкости."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from identity_hub import IdentityCreate, IdentityHub, SpaceHandle

pytestmark = pytest.mark.pg


async def test_eligible_count_accepts_read_schema_select(hub: IdentityHub, space: SpaceHandle) -> None:
    created = await space.identities.create_many(
        [
            IdentityCreate(
                service="fake_tg",
                external_ref=f"+eligible-select-{index}",
                lifecycle="ready",
                session={"auth_key": "k"},
            )
            for index in range(2)
        ],
        actor="test",
    )
    wanted = created.created[1].id
    stmt = select(hub.read.identities.c.id).where(hub.read.identities.c.id == wanted)
    pool = space.pools.define("eligible-select", service="fake_tg")

    capacity = await pool.eligible_count(among=stmt)

    assert capacity.eligible == capacity.free == 1
    lease = await pool.acquire(among=stmt)
    leased = await pool.eligible_count(among=stmt)
    assert leased.eligible == leased.leased == 1
    assert leased.free == 0
    await lease.release()
