"""PA2.3: окончание use освобождает временное место, сохраняя постоянную пару."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, update
from tests.fixtures.api import register

from identity_hub import (
    LeaseLost,
    LeaseOwnershipLost,
    LeaseRevoked,
    LeaseUnavailable,
    ProxyBindingPolicy,
    ProxyPolicy,
    SpaceHandle,
)
from identity_hub.clock import FakeClock
from identity_hub.postgres.models import LeaseHistoryRecord, LeaseRecord
from identity_hub.proxy_allocator import reserve

pytestmark = pytest.mark.conformance


async def _setup(space, state="reserved", *, max_hold=None, short=False):
    proxy = await space.proxies.create("http://127.0.0.1:8986", group="g", max_identities=1)
    await space.proxies.configure_group("g", capacity_accounting="allocations", actor="test", reason="enable")
    identity = (
        await register(
            space, service="fake_tg", external_ref="a", lifecycle="ready", session={"auth_key": "k"}
        )
    ).identity_id
    policy = ProxyPolicy(group="g", binding=ProxyBindingPolicy())
    timing = (
        {
            "ttl": timedelta(seconds=1),
            "heartbeat": timedelta(seconds=0.1),
            "safety_margin": timedelta(seconds=0.2),
        }
        if short or max_hold is not None
        else {}
    )
    pool = space.pools.define("p", service="fake_tg", holder="worker", proxy_policy=policy, **timing)
    lease = await pool.acquire(among=[identity], max_hold=max_hold)
    if state == "bound":
        await lease.confirm_proxy(reason="accepted")
    elif state == "draining":
        await lease.confirm_proxy(reason="accepted")
        await space.proxies.unbind(identity, actor="admin", reason="invalid")
    return proxy, identity, pool, lease


async def _expire(hub, lease, sessionmaker):
    if isinstance(hub.clock, FakeClock):
        hub.clock.advance(lease.expires_at - hub.clock.now() + timedelta(seconds=1))
    else:
        async with sessionmaker.begin() as session:
            await session.execute(
                update(LeaseRecord)
                .where(LeaseRecord.lease_id == lease.lease_id)
                .values(expires_at=func.now() - timedelta(seconds=1))
            )


async def _assert_end(space, proxy, lease, state):
    expected = "bound" if state == "bound" else "released"
    async with space._tx() as tx:
        row = await tx.proxy_allocation(lease.proxy_allocation_id)
        assert row.state == expected and row.owner_lease_id is None
        resource = await tx.get_proxy(proxy.id)
        assert (resource.bound_count, resource.reserved_count, resource.draining_count) == (
            int(expected == "bound"),
            0,
            0,
        )
        bindings = await tx.proxy_bindings(identity_id=lease.identity_id)
        assert len(bindings) == int(expected == "bound")


@pytest.mark.parametrize("state", ["reserved", "draining", "bound"])
async def test_revoke_closes_use_in_same_transaction(space: SpaceHandle, state):
    proxy, identity, _, lease = await _setup(space, state)
    assert await space.pools.revoke(identity, actor="admin", reason="stop")
    await _assert_end(space, proxy, lease, state)
    with pytest.raises((LeaseOwnershipLost, LeaseLost)):
        await lease.confirm_proxy(reason="late check")
    with pytest.raises(LeaseRevoked):
        await lease.release()


@pytest.mark.parametrize("state", ["reserved", "draining", "bound"])
async def test_expiry_closes_use_once_without_worker(
    space: SpaceHandle, hub_under_test, backend_sessionmaker, state
):
    proxy, _, _, lease = await _setup(space, state)
    await _expire(hub_under_test, lease, backend_sessionmaker)
    async with space._tx() as tx:
        assert await tx.expire_leases(10) == 1
    await _assert_end(space, proxy, lease, state)
    async with space._tx() as tx:
        assert await tx.expire_leases(10) == 0
    with pytest.raises((LeaseOwnershipLost, LeaseLost)):
        await lease.confirm_proxy(reason="late check")


@pytest.mark.parametrize("new_policy", ["none", "deferred"])
@pytest.mark.parametrize("state", ["reserved", "draining", "bound"])
async def test_reclaim_closes_old_use_before_overwrite_and_preserves_new_reserve(
    space: SpaceHandle, hub_under_test, backend_sessionmaker, state, new_policy
):
    proxy, identity, pool, lease = await _setup(space, state)
    await _expire(hub_under_test, lease, backend_sessionmaker)
    if new_policy == "none":
        pool = space.pools.define("plain", service="fake_tg", holder="replacement")
    replacement = await pool.acquire(among=[identity])
    async with space._tx() as tx:
        old = await tx.proxy_allocation(lease.proxy_allocation_id)
        assert old.state == ("bound" if state == "bound" else "released")
        if new_policy == "deferred":
            new = await tx.proxy_allocation(replacement.proxy_allocation_id)
            assert new.state == ("bound" if state == "bound" else "reserved")
            resource = await tx.get_proxy(proxy.id)
            assert resource.bound_count + resource.reserved_count + resource.draining_count == 1
        else:
            assert replacement.proxy_allocation_id is None
    # Запоздалое завершение старого lease не затрагивает use нового владельца.
    await lease.release()
    if new_policy == "deferred":
        assert (await replacement.confirm_proxy(reason="new check")).state == "bound"
    await replacement.release()


async def test_failed_revoke_rolls_back_allocation_and_history(space: SpaceHandle, monkeypatch):
    proxy, identity, _, lease = await _setup(space)
    async with space._tx() as tx:
        backend = type(tx)
    original = backend.revoke_lease

    async def fail(self, identity_id):
        await original(self, identity_id)
        raise RuntimeError("after revoke")

    monkeypatch.setattr(backend, "revoke_lease", fail)
    with pytest.raises(RuntimeError, match="after revoke"):
        await space.pools.revoke(identity, actor="admin", reason="stop")
    async with space._tx() as tx:
        assert (await tx.proxy_allocation(lease.proxy_allocation_id)).state == "reserved"
        assert (await tx.get_proxy(proxy.id)).reserved_count == 1
        await tx.check_fence(lease.fence)
    await lease.release()


@pytest.mark.parametrize("limited", [False, True], ids=["heartbeat", "max_hold"])
async def test_reservation_uses_canonical_lease_renewal_and_hold_deadline(
    space: SpaceHandle, hub_under_test, limited
):
    proxy, _, _, lease = await _setup(space, max_hold=timedelta(seconds=1) if limited else None, short=True)
    renewal = (await hub_under_test._store.renew_leases([lease._row.token], 60))[lease._row.token]
    assert renewal.expires_at == lease.hold_deadline if limited else renewal.expires_at > lease.expires_at
    if isinstance(hub_under_test.clock, FakeClock):
        hub_under_test.clock.advance(timedelta(seconds=1.1))
    else:
        await asyncio.sleep(1.1)
    async with space._tx() as tx:
        assert await tx.expire_leases(10) == int(limited)
        if limited:
            history = await tx.lease_read_rows(lease.acquired_at - timedelta(seconds=1))
            assert next(row for row in history if row.lease_id == lease.lease_id).end_reason == "max_hold"
        else:
            assert (await tx.proxy_allocation(lease.proxy_allocation_id)).state == "reserved"
            assert (await tx.get_proxy(proxy.id)).reserved_count == 1
    if limited:
        await _assert_end(space, proxy, lease, "reserved")
    await lease.release()


async def test_forced_yield_returns_slot_before_next_acquire(space: SpaceHandle, hub_under_test):
    proxy, identity, _, lease = await _setup(space)
    urgent = space.pools.define(
        "urgent", service="fake_tg", holder="urgent", priority=10, yield_grace=timedelta(milliseconds=1)
    )
    # Первый запрос ставит yield_requested_at, следующий принудительно завершает.
    with pytest.raises(LeaseUnavailable):
        await urgent.acquire(among=[identity])
    if isinstance(hub_under_test.clock, FakeClock):
        hub_under_test.clock.advance(timedelta(milliseconds=2))
    else:
        await asyncio.sleep(0.01)
    with pytest.raises(LeaseUnavailable):
        await urgent.acquire(among=[identity])
    await _assert_end(space, proxy, lease, "reserved")
    replacement = await urgent.acquire(among=[identity])
    await replacement.release()


async def test_retention_keeps_history_referenced_by_active_allocation(
    space: SpaceHandle, hub_under_test, backend_sessionmaker
):
    _, _, _, lease = await _setup(space)
    # Моделируем историю, закрытую прежним runtime до согласования ledger.
    if backend_sessionmaker is None:
        store = hub_under_test._store
        store._state.history[lease.lease_id] = replace(
            store._state.history[lease.lease_id], released_at=hub_under_test.clock.now()
        )
    else:
        async with backend_sessionmaker.begin() as session:
            await session.execute(
                update(LeaseHistoryRecord)
                .where(LeaseHistoryRecord.lease_id == lease.lease_id)
                .values(released_at=func.now(), end_reason="released")
            )
    retention = {"lease_history": timedelta(days=-1)}
    counts = await hub_under_test._store.cleanup_maintenance(retention, frozenset({"lease_history"}))
    assert counts["lease_history"] == 0
    await lease.release()
    counts = await hub_under_test._store.cleanup_maintenance(retention, frozenset({"lease_history"}))
    assert counts["lease_history"] == 1


async def test_abandoned_worker_slot_returns_to_another_identity_after_expire(
    space: SpaceHandle, hub_under_test, backend_sessionmaker
):
    proxy, _, pool, old = await _setup(space)
    other = (
        await register(
            space, service="fake_tg", external_ref="other", lifecycle="ready", session={"auth_key": "b"}
        )
    ).identity_id
    await _expire(hub_under_test, old, backend_sessionmaker)
    # Умерший воркер больше не посылает heartbeat/release. До cleanup место
    # консервативно занято; выдавать его поверх ledger нельзя.
    with pytest.raises(LeaseUnavailable):
        async with space._tx() as tx:
            await reserve(tx, other, "g", owner_lease_id=uuid4())
    async with space._tx() as tx:
        assert (await tx.get_proxy(proxy.id)).reserved_count == 1
        assert await tx.expire_leases(10) == 1
    replacement = await pool.acquire(among=[other])
    assert replacement.proxy_id == old.proxy_id
    await old.release()
    async with space._tx() as tx:
        assert (await tx.proxy_allocation(replacement.proxy_allocation_id)).state == "reserved"
        assert (await tx.get_proxy(proxy.id)).reserved_count == 1
    await replacement.release()
