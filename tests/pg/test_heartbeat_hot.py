"""IMP-02 (I2.2a): heartbeat — HOT-обновление строки аренды (D-210)."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from identity_hub import IdentityCreate
from identity_hub.hub import SpaceHandle

pytestmark = [pytest.mark.anyio, pytest.mark.pg]

LEASES, ROUNDS = 30, 10
STATS = text("SELECT n_tup_upd, n_tup_hot_upd FROM pg_stat_user_tables WHERE relname = 'ih_leases'")


async def _stats(db: AsyncEngine) -> tuple[int, int]:
    async with db.connect() as conn:
        await conn.execute(text("SELECT pg_stat_force_next_flush()"))
        row = (await conn.execute(STATS)).one()
    return int(row.n_tup_upd), int(row.n_tup_hot_upd)


async def test_heartbeat_is_hot_update(space: SpaceHandle, db: AsyncEngine) -> None:
    await space.identities.create_many(
        [
            IdentityCreate(
                service="fake_tg", external_ref=f"+{n}", lifecycle="ready", session={"auth_key": "k"}
            )
            for n in range(LEASES)
        ],
        actor="test",
    )
    pool = space.pools.define("hot", service="fake_tg", holder="w")
    leases = [await pool.acquire() for _ in range(LEASES)]
    await asyncio.sleep(1.1)  # статистика сессий сбрасывается не чаще раза в секунду
    before = await _stats(db)

    for _ in range(ROUNDS):
        for lease in leases:
            assert await lease._extend() is not None
    expected = LEASES * ROUNDS
    for _ in range(40):  # ждём, пока backend-ы пула сбросят свою статистику
        await asyncio.sleep(0.5)
        after = await _stats(db)
        if after[0] - before[0] >= expected:
            break
    updates, hot = after[0] - before[0], after[1] - before[1]

    assert updates >= expected
    assert hot / updates >= 0.9, (hot, updates)
    for lease in leases:
        await lease.release()


async def test_lease_indexes_do_not_cover_heartbeat_columns(db: AsyncEngine) -> None:
    async with db.connect() as conn:
        definitions = (
            (
                await conn.execute(
                    text(
                        "SELECT indexdef FROM pg_indexes WHERE tablename IN ('ih_leases', 'ih_lease_history')"
                    )
                )
            )
            .scalars()
            .all()
        )
        options = (
            await conn.execute(text("SELECT reloptions FROM pg_class WHERE relname = 'ih_leases'"))
        ).scalar_one()
    assert not [d for d in definitions if "expires_at" in d or "heartbeat" in d]
    assert "fillfactor=70" in (options or [])
