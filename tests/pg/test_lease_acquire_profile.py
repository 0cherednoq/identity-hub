"""I6.4: профиль SQL round trips одной аренды — основа бюджета §10.1."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

from identity_hub import IdentityCreate, IdentityHub, Lifecycle
from identity_hub.hub import SpaceHandle

pytestmark = pytest.mark.pg


async def test_acquire_round_trip_profile(hub: IdentityHub, db: AsyncEngine, space: SpaceHandle) -> None:
    for index in range(3):
        await space.identities.create(
            IdentityCreate(
                service="fake_tg",
                external_ref=f"+7940000{index:04d}",
                session={"auth_key": "k"},
                lifecycle=Lifecycle.READY,
            ),
            actor="test",
        )
    pool = space.pools.define("profile", service="fake_tg")
    warm = await pool.acquire()
    await warm.release()

    statements: list[str] = []

    def capture(_conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        statements.append(statement)

    event.listen(db.sync_engine, "before_cursor_execute", capture)
    try:
        lease = await pool.acquire()
        acquired = len(statements)
        await lease.release()
        released = len(statements) - acquired
    finally:
        event.remove(db.sync_engine, "before_cursor_execute", capture)

    print(f"PROFILE acquire={acquired} release={released}")
    for statement in statements[:acquired]:
        print("ACQUIRE:", " ".join(statement.split())[:150])
    assert acquired > 0
