"""P2.2: transactional outbox (ТЗ FR-13.1, G-4.1, G-5.1)."""

from __future__ import annotations

import asyncio
import os
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from identity_hub._ids import new_id
from identity_hub.events import EventDraft, SecretLeak
from identity_hub.hub import SpaceHandle
from identity_hub.postgres.models import EventRecord, IdentityRecord
from identity_hub.postgres.store import NOTIFY_CHANNEL

pytestmark = pytest.mark.pg


async def _identity(space: SpaceHandle) -> UUID:
    ref = await space.ref()
    identity_id = new_id()
    async with space._tx() as tx:
        await tx.execute(  # type: ignore[attr-defined]
            IdentityRecord.__table__.insert().values(
                id=identity_id, space_id=ref.id, service="fake_tg", external_ref=str(identity_id)
            )
        )
    return identity_id


async def _events(sessionmaker: async_sessionmaker[AsyncSession]) -> list[Any]:
    async with sessionmaker() as s:
        return list((await s.execute(select(EventRecord).order_by(EventRecord.id))).scalars())


async def test_event_visible_only_after_commit(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    identity_id = await _identity(space)
    async with space._tx() as tx:
        await space._emit(
            tx, EventDraft("identity.updated", actor="w", identity_id=identity_id, data={"a": 1})
        )
        assert await _events(sessionmaker) == []
    (event,) = await _events(sessionmaker)
    assert event.type == "identity.updated"
    assert event.sequence == 1
    assert event.external is True
    assert event.data == {"a": 1}
    assert event.tx_id is not None


async def test_rollback_drops_events(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await space.ref()
    with pytest.raises(RuntimeError):
        async with space._tx() as tx:
            await space._emit(tx, EventDraft("pool.capacity.low", actor="hub"))
            raise RuntimeError("rollback")
    assert await _events(sessionmaker) == []


async def test_sequence_per_identity(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    a, b = await _identity(space), await _identity(space)
    for identity_id in (a, a, b, a):
        async with space._tx() as tx:
            await space._emit(tx, EventDraft("identity.updated", actor="w", identity_id=identity_id))
    events = await _events(sessionmaker)
    assert [(e.identity_id, e.sequence) for e in events] == [(a, 1), (a, 2), (b, 1), (a, 3)]
    async with sessionmaker() as s:
        seq = (await s.execute(select(IdentityRecord.event_seq).where(IdentityRecord.id == a))).scalar_one()
    assert seq == 3


async def test_journal_only_events_and_severity(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await space.ref()
    async with space._tx() as tx:
        await space._emit(
            tx,
            EventDraft("lease.acquired", actor="w"),
            EventDraft("pool.capacity.empty", actor="hub"),
        )
    journal, capacity = await _events(sessionmaker)
    assert journal.external is False
    assert capacity.severity == "critical"


async def test_secrets_never_reach_journal(space: SpaceHandle) -> None:
    await space.ref()
    with pytest.raises(SecretLeak):
        async with space._tx() as tx:
            await space._emit(tx, EventDraft("identity.updated", actor="w", data={"password": "x"}))


@pytest.mark.skipif(
    os.environ.get("IH_TEST_PGBOUNCER") == "1",
    reason="LISTEN через PgBouncer в режиме транзакций не доставляется (F4.5, проверяется в test_pgbouncer)",
)
async def test_notify_on_commit(space: SpaceHandle, db: AsyncEngine) -> None:
    await space.ref()
    received: asyncio.Queue[str] = asyncio.Queue()
    async with db.connect() as conn:
        raw = await conn.get_raw_connection()
        driver = raw.driver_connection
        assert driver is not None

        def listener(*args: Any) -> None:
            received.put_nowait(args[-1])

        await driver.add_listener(NOTIFY_CHANNEL, listener)
        try:
            async with space._tx() as tx:
                await space._emit(tx, EventDraft("pool.capacity.low", actor="hub"))
                await asyncio.sleep(0.2)
                assert received.empty()  # до коммита уведомления нет
            assert await asyncio.wait_for(received.get(), timeout=5) == "tennet"
        finally:
            await driver.remove_listener(NOTIFY_CHANNEL, listener)
