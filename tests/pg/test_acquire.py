"""P3.2: захват аренды (ТЗ FR-6.1, 6.4–6.8, G-1.1, G-4.2)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub.errors import LeaseUnavailable
from identity_hub.hub import IdentityHub, SpaceHandle
from identity_hub.postgres.models import EventRecord, IdentityRecord, LeaseHistoryRecord, LeaseRecord
from identity_hub.service import LeaseMode
from tests.fixtures.services import FAKE_TG
from tests.pg.helpers import make_identity

pytestmark = pytest.mark.pg


async def _all(sm: async_sessionmaker[AsyncSession], model: Any) -> list[Any]:
    async with sm() as s:
        return list((await s.execute(select(model))).scalars())


async def test_acquire_writes_lease_history_and_event(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    identity_id = await make_identity(space)
    lease = await space.pool("parsers", service="fake_tg", holder="host:1").acquire()
    assert lease.identity_id == identity_id
    assert lease.fencing == 1
    assert "token" not in repr(lease) and str(lease._token) not in repr(lease)

    (row,) = await _all(sessionmaker, LeaseRecord)
    assert (row.lease_id, row.fencing, row.pool, row.holder) == (lease.lease_id, 1, "parsers", "host:1")
    assert row.expires_at == lease.expires_at
    (history,) = await _all(sessionmaker, LeaseHistoryRecord)
    assert (history.lease_id, history.token, history.released_at) == (lease.lease_id, lease._token, None)
    (event,) = await _all(sessionmaker, EventRecord)
    assert (event.type, event.external, event.lease_id) == ("lease.acquired", False, lease.lease_id)
    assert str(lease._token) not in str(event.data)
    async with sessionmaker() as s:
        seq = (await s.execute(select(IdentityRecord.fencing_seq))).scalar_one()
    assert seq == 1


async def test_race_many_acquirers_are_exclusive(space: SpaceHandle) -> None:
    ids = {await make_identity(space) for _ in range(5)}
    pool = space.pool("p", service="fake_tg")
    results = await asyncio.gather(*(pool.acquire() for _ in range(12)), return_exceptions=True)
    leases = [r for r in results if not isinstance(r, BaseException)]
    errors = [r for r in results if isinstance(r, BaseException)]
    assert len(leases) == 5
    assert {lease.identity_id for lease in leases} == ids
    assert all(isinstance(e, LeaseUnavailable) for e in errors)


async def test_race_50_acquirers_do_not_lose_available_candidates(space: SpaceHandle) -> None:
    ids = {await make_identity(space) for _ in range(50)}
    pool = space.pool("p50", service="fake_tg")

    results = await asyncio.gather(*(pool.acquire() for _ in range(50)), return_exceptions=True)

    assert not [result for result in results if isinstance(result, BaseException)]
    leases = [result for result in results if not isinstance(result, BaseException)]
    assert {lease.identity_id for lease in leases} == ids


async def test_idempotency_key_returns_same_lease(space: SpaceHandle) -> None:
    await make_identity(space)
    await make_identity(space)
    pool = space.pool("p", service="fake_tg")
    first = await pool.acquire(idempotency_key="job-42")
    again = await pool.acquire(idempotency_key="job-42")
    assert again.lease_id == first.lease_id
    other = await pool.acquire(idempotency_key="job-43")
    assert other.identity_id != first.identity_id


async def test_expired_lease_reclaimed_with_history_closed_at_expiry(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await make_identity(space)
    pool = space.pool("p", service="fake_tg")
    old = await pool.acquire(idempotency_key="k")
    async with sessionmaker() as s, s.begin():
        await s.execute(update(LeaseRecord).values(expires_at=text("now() - interval '5 seconds'")))
        expired_at = (await s.execute(select(LeaseRecord.expires_at))).scalar_one()
    new = await pool.acquire(idempotency_key="k")  # ключ истёкшей аренды не мешает
    assert new.lease_id != old.lease_id
    assert new.fencing == old.fencing + 1
    history = {h.lease_id: h for h in await _all(sessionmaker, LeaseHistoryRecord)}
    assert history[old.lease_id].end_reason == "expired"
    assert history[old.lease_id].released_at == expired_at  # FR-6.8
    assert history[old.lease_id].duration is not None
    assert history[new.lease_id].released_at is None
    types = [e.type for e in await _all(sessionmaker, EventRecord)]
    assert types.count("lease.expired") == 1


async def test_wait_for_free_identity(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await make_identity(space)
    pool = space.pool("p", service="fake_tg")
    await pool.acquire()
    with pytest.raises(LeaseUnavailable):
        await pool.acquire()
    with pytest.raises(LeaseUnavailable):
        await pool.acquire(wait=timedelta(milliseconds=300))

    async def free_later() -> None:
        await asyncio.sleep(0.3)
        async with sessionmaker() as s, s.begin():
            await s.execute(update(LeaseRecord).values(expires_at=text("now()")))

    task = asyncio.create_task(free_later())
    lease = await pool.acquire(wait=5)
    await task
    assert lease.fencing == 2


async def test_shared_mode_does_not_block(
    hub: IdentityHub, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    hub.register_service(replace(FAKE_TG, name="imap", lease_mode=LeaseMode.SHARED))
    await hub.start()
    space = hub.space("s")
    await make_identity(space, service="imap")
    pool = space.pool("mail", service="imap")
    a, b = await pool.acquire(), await pool.acquire()
    assert a.identity_id == b.identity_id
    assert a.lease_id != b.lease_id
    assert await _all(sessionmaker, LeaseRecord) == []
    assert len(await _all(sessionmaker, LeaseHistoryRecord)) == 2
