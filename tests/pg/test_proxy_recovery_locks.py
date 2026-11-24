"""PA4.1: skip locks не ждёт пользователя и перепроверяет canonical heartbeat."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, update

from identity_hub import ProxyBindingPolicy, ProxyPolicy
from identity_hub.postgres.models import LeaseRecord
from identity_hub.postgres.store import PgTx
from tests.fixtures.api import register

pytestmark = pytest.mark.pg


async def _setup(space, count=1):
    proxy = await space.proxies.create("http://127.0.0.1:9010", group="g", max_identities=count)
    await space.proxies.configure_group("g", capacity_accounting="allocations", actor="test", reason="enable")
    ids = [
        (
            await register(
                space, service="fake_tg", external_ref=str(n), lifecycle="ready", session={"auth_key": "k"}
            )
        ).identity_id
        for n in range(count)
    ]
    pool = space.pools.define(
        "p",
        service="fake_tg",
        holder="worker",
        proxy_policy=ProxyPolicy(group="g", binding=ProxyBindingPolicy()),
    )
    leases = [await pool.acquire(among=[identity]) for identity in ids]
    return proxy, leases


@pytest.mark.parametrize("resource", ["identity", "proxy"])
async def test_cleanup_skips_locked_resource_and_revisits_after_unlock(space, sessionmaker, resource):
    proxy, (lease,) = await _setup(space)
    async with sessionmaker.begin() as session:
        await session.execute(
            update(LeaseRecord)
            .where(LeaseRecord.lease_id == lease.lease_id)
            .values(expires_at=func.now() - timedelta(seconds=1))
        )
    async with space._tx() as holding:
        if resource == "identity":
            await holding.lock_identities([lease.identity_id])
        else:
            await holding.proxy_lock_rows([proxy.id])
        async with asyncio.timeout(5):
            assert await space.maintenance.cleanup_proxy_allocations(limit=1) == 0
        assert (await holding.proxy_allocation(lease.proxy_allocation_id)).state == "reserved"
    assert await space.maintenance.cleanup_proxy_allocations(limit=1) == 1
    async with space._tx() as tx:
        assert (await tx.get_proxy(proxy.id)).reserved_count == 0


async def test_fixed_discovery_budget_skips_locked_head_without_unbounded_scan(
    space, sessionmaker, monkeypatch
):
    _, leases = await _setup(space, count=3)
    async with sessionmaker.begin() as session:
        await session.execute(update(LeaseRecord).values(expires_at=func.now() - timedelta(seconds=1)))
    original = PgTx.proxy_cleanup_page
    seen = []

    async def page(tx, limit, **kwargs):
        rows, until = await original(tx, limit, **kwargs)
        assert len(rows) <= limit
        seen.extend(row.id for row in rows)
        return rows, until

    monkeypatch.setattr(PgTx, "proxy_cleanup_page", page)
    async with space._tx() as holding:
        await holding.lock_identities([lease.identity_id for lease in leases[:2]])
        async with asyncio.timeout(5):
            assert await space.maintenance.cleanup_proxy_allocations(limit=1) == 0
            assert await space.maintenance.cleanup_proxy_allocations(limit=1) == 0
            assert await space.maintenance.cleanup_proxy_allocations(limit=1) == 1
    assert seen == [lease.proxy_allocation_id for lease in leases]
    assert await space.maintenance.cleanup_proxy_allocations(limit=2) == 2


async def test_heartbeat_after_discovery_is_authoritative(space, hub, monkeypatch):
    proxy, (lease,) = await _setup(space)
    original = PgTx.proxy_cleanup_lock_rows
    entered, proceed = asyncio.Event(), asyncio.Event()

    async def barrier(tx, rows):
        entered.set()
        await proceed.wait()
        return await original(tx, rows)

    monkeypatch.setattr(PgTx, "proxy_cleanup_lock_rows", barrier)
    task = asyncio.create_task(space.maintenance.cleanup_proxy_allocations(limit=1))
    async with asyncio.timeout(10):
        await entered.wait()
        renewal = (await hub._store.renew_leases([lease._row.token], 120))[lease._row.token]
        assert renewal.expires_at is not None
        proceed.set()
        assert await task == 0
    async with space._tx() as tx:
        current = await tx.current_lease(lease.identity_id)
        assert current.expires_at == renewal.expires_at
        assert (await tx.proxy_allocation(lease.proxy_allocation_id)).state == "reserved"
        assert (await tx.get_proxy(proxy.id)).reserved_count == 1
    await lease.release()


async def test_confirm_after_discovery_keeps_permanent_binding(space, monkeypatch):
    proxy, (lease,) = await _setup(space)
    original = PgTx.proxy_cleanup_lock_rows
    entered, proceed = asyncio.Event(), asyncio.Event()

    async def barrier(tx, rows):
        entered.set()
        await proceed.wait()
        return await original(tx, rows)

    monkeypatch.setattr(PgTx, "proxy_cleanup_lock_rows", barrier)
    task = asyncio.create_task(space.maintenance.cleanup_proxy_allocations(limit=1))
    async with asyncio.timeout(10):
        await entered.wait()
        await lease.confirm_proxy(reason="accepted result")
        proceed.set()
        assert await task == 0
    await lease.release()
    async with space._tx() as tx:
        assert (await tx.proxy_allocation(lease.proxy_allocation_id)).state == "bound"
        assert (await tx.get_proxy(proxy.id)).bound_count == 1


async def test_counter_repair_skips_proxy_lock_and_revisits(space, hub):
    proxy, (lease,) = await _setup(space)
    await lease.release()
    async with space._tx() as tx:
        await tx.proxy_set_counters(proxy.id, bound=0, reserved=1, draining=0)
    async with space._tx() as holding:
        await holding.proxy_lock_rows([proxy.id])
        async with asyncio.timeout(5):
            assert await hub._proxy_recovery.repair_counters(space, limit=1) == 0
    assert await hub._proxy_recovery.repair_counters(space, limit=1) == 1
