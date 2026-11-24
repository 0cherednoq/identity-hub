"""PA5/R14: orphan history не ждёт busy row и не закрывает новую generation."""

import asyncio

import pytest
from sqlalchemy import delete, select

from identity_hub.postgres.models import LeaseHistoryRecord, LeaseRecord
from identity_hub.postgres.store import PgTx
from identity_hub.read.events import EventFilters
from tests.conformance.test_proxy_allocation_recovery import _setup

pytestmark = pytest.mark.pg


async def _orphan(space, sessionmaker):
    proxy, pool, (lease,) = await _setup(space)
    await lease.confirm_proxy(reason="accepted")
    async with sessionmaker.begin() as session:
        await session.execute(delete(LeaseRecord).where(LeaseRecord.lease_id == lease.lease_id))
    return proxy, pool, lease


@pytest.mark.parametrize("resource", ["identity", "history"])
async def test_orphan_history_skips_busy_row_and_revisits(space, hub, sessionmaker, resource):
    _, _, lease = await _orphan(space, sessionmaker)
    async with sessionmaker.begin() as session:
        if resource == "history":
            await session.execute(
                select(LeaseHistoryRecord.lease_id)
                .where(LeaseHistoryRecord.lease_id == lease.lease_id)
                .with_for_update()
            )
        async with space._tx() as tx:
            if resource == "identity":
                await tx.lock_identities([lease.identity_id])
            async with asyncio.timeout(5):
                assert await hub._proxy_recovery.close_orphan_history(space, limit=1) == 0
    assert await hub._proxy_recovery.close_orphan_history(space, limit=1) == 1


async def test_history_only_recovery_does_not_take_proxy_lock(space, hub, sessionmaker):
    proxy, _, _ = await _orphan(space, sessionmaker)
    async with space._tx() as tx:
        await tx.proxy_lock_rows([proxy.id])
        async with asyncio.timeout(5):
            assert await hub._proxy_recovery.close_orphan_history(space, limit=1) == 1
        assert (await tx.get_proxy(proxy.id)).bound_count == 1


async def test_new_use_after_discovery_survives_old_history_close(space, hub, sessionmaker, monkeypatch):
    _, pool, old = await _orphan(space, sessionmaker)
    original = PgTx.proxy_close_orphan_history
    discovered, proceed = asyncio.Event(), asyncio.Event()

    async def barrier(tx, rows):
        discovered.set()
        await proceed.wait()
        return await original(tx, rows)

    monkeypatch.setattr(PgTx, "proxy_close_orphan_history", barrier)
    task = asyncio.create_task(hub._proxy_recovery.close_orphan_history(space, limit=1))
    async with asyncio.timeout(10):
        await discovered.wait()
        current = await pool.acquire(among=[old.identity_id])
        proceed.set()
        assert await task == 1
        await current.confirm_proxy(reason="new generation still owned")
    async with space._tx() as tx:
        assert (await tx.current_lease(old.identity_id)).lease_id == current.lease_id
        history = await tx.lease_history(old.identity_id, limit=10, before=None)
        assert next(row for row in history if row.lease_id == current.lease_id).released_at is None
    await current.release()


async def test_two_sweepers_close_one_history_and_emit_one_event(space, hub, sessionmaker, monkeypatch):
    _, _, _ = await _orphan(space, sessionmaker)
    original = PgTx.proxy_close_orphan_history
    ready = asyncio.Event()
    arrivals = 0

    async def barrier(tx, rows):
        nonlocal arrivals
        arrivals += 1
        if arrivals == 2:
            ready.set()
        await ready.wait()
        return await original(tx, rows)

    monkeypatch.setattr(PgTx, "proxy_close_orphan_history", barrier)
    async with asyncio.timeout(10):
        results = await asyncio.gather(
            *(hub._proxy_recovery.close_orphan_history(space, limit=1) for _ in range(2))
        )
    assert sum(results) == 1
    events = await space.admin.event_page(filters=EventFilters(type="lease.lost", include_journal=True))
    assert len(events.items) == 1
