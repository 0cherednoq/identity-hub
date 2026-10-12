"""IMP-10 (I3.3b): параллельная замена сессии — одна активная запись, история цела (D-218, D-219)."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub import IdentityCreate
from identity_hub.crypto import associated_data
from identity_hub.hub import SpaceHandle
from tests.fixtures.services import TgSession

pytestmark = [pytest.mark.pg]


async def _wait_blocked(sessionmaker: async_sessionmaker[AsyncSession], task: asyncio.Task[object]) -> None:
    blocked = text("SELECT count(*) FROM pg_locks WHERE NOT granted")
    async with sessionmaker() as session:
        for _ in range(500):
            assert not task.done(), "вторая замена не дождалась первой"
            if (await session.execute(blocked)).scalar_one():
                return
            await asyncio.sleep(0.01)
    pytest.fail("вторая замена не встала на блокировку")


async def test_concurrent_replace_keeps_one_active(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    created = await space.identities.create(
        IdentityCreate(service="fake_tg", external_ref="+race", session={"auth_key": "k0"}), actor="test"
    )
    aad = associated_data("ih_sessions", created.id, "default")
    envelope = space.hub._envelope
    held, go = asyncio.Event(), asyncio.Event()

    async def first() -> None:
        async with space._tx(own=True) as tx:
            payload = envelope.seal_model(TgSession(auth_key="k1"), aad)  # type: ignore[arg-type]
            assert await tx.put_session(created.id, "default", payload, fencing=0, mode="replace")
            held.set()
            await go.wait()

    winner = asyncio.create_task(first())
    await held.wait()
    second = asyncio.create_task(
        space.secrets.replace_session(created.id, {"auth_key": "k2"}, actor="ops", mode="replace")
    )
    await _wait_blocked(sessionmaker, second)
    go.set()
    await winner
    await second

    async with space._tx(own=True) as tx:
        history = await tx.session_history(created.id, "default")
    assert [row.status for row in history] == ["revoked", "revoked", "active"]
    assert [row.revision for row in history] == [1, 2, 3]
