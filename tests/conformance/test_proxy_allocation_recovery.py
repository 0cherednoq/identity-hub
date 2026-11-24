"""PA4.1: bounded recovery, fairness цикла и возврат чужого брошенного слота."""

from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import delete, func, update
from tests.fixtures.api import register

from identity_hub import LeaseLost, LeaseOwnershipLost, ProxyBindingPolicy, ProxyPolicy, ValidationFailed
from identity_hub.clock import FakeClock
from identity_hub.lease import Lease, _CandidateBudget, acquire
from identity_hub.postgres.models import LeaseHistoryRecord, LeaseRecord
from identity_hub.proxy_recovery import ProxyRecovery

pytestmark = pytest.mark.conformance


async def _setup(space, *, count=1, group="g", start=9000):
    proxy = await space.proxies.create(f"http://127.0.0.1:{start}", group=group, max_identities=count)
    await space.proxies.configure_group(
        group, capacity_accounting="allocations", actor="test", reason="enable"
    )
    identities = [
        (
            await register(
                space,
                service="fake_tg",
                external_ref=f"{group}-{n}",
                lifecycle="ready",
                session={"auth_key": "k"},
            )
        ).identity_id
        for n in range(count)
    ]
    pool = space.pools.define(
        group,
        service="fake_tg",
        holder="worker",
        proxy_policy=ProxyPolicy(group=group, binding=ProxyBindingPolicy()),
    )
    return proxy, pool, [await pool.acquire(among=[identity]) for identity in identities]


async def _expire(hub, leases, sessions):
    if isinstance(hub.clock, FakeClock):
        hub.clock.advance(max(lease.expires_at for lease in leases) - hub.clock.now() + timedelta(seconds=1))
    else:
        async with sessions.begin() as session:
            await session.execute(
                update(LeaseRecord)
                .where(LeaseRecord.lease_id.in_([lease.lease_id for lease in leases]))
                .values(expires_at=func.now() - timedelta(seconds=1))
            )


async def _state(space, lease):
    async with space._tx() as tx:
        row = await tx.proxy_allocation(lease.proxy_allocation_id)
        return row.state if row else None


@pytest.mark.parametrize("internal_budget", [False, True])
async def test_lazy_recovery_returns_other_identity_slot_without_periodic_job(
    space, hub_under_test, backend_sessionmaker, internal_budget
):
    proxy, pool, (old,) = await _setup(space)
    other = (
        await register(
            space, service="fake_tg", external_ref="other", lifecycle="ready", session={"auth_key": "b"}
        )
    ).identity_id
    await _expire(hub_under_test, [old], backend_sessionmaker)
    replacement = await acquire(
        pool, among=[other], candidate_budget=_CandidateBudget(1) if internal_budget else None
    )
    assert replacement.proxy_id == proxy.id and await _state(space, old) == "released"
    await old.release()
    assert await _state(space, replacement) == "reserved"
    await replacement.release()


async def test_renewed_live_owner_survives_and_batch_is_bounded(space, hub_under_test, backend_sessionmaker):
    proxy, _, leases = await _setup(space, count=3)
    assert (await hub_under_test._store.renew_leases([leases[0]._row.token], 120))[
        leases[0]._row.token
    ].expires_at is not None
    await _expire(hub_under_test, leases[1:], backend_sessionmaker)
    assert await space.maintenance.cleanup_proxy_allocations(limit=1) == 0
    assert await space.maintenance.cleanup_proxy_allocations(limit=1) == 1
    assert await space.maintenance.cleanup_proxy_allocations(limit=1) == 1
    assert await _state(space, leases[0]) == "reserved"
    async with space._tx() as tx:
        assert (await tx.get_proxy(proxy.id)).reserved_count == 1
    for lease in leases:
        await lease.release()


async def test_group_and_space_scoping(space, hub_under_test, backend_sessionmaker):
    await space.proxies.configure_group("b", capacity_accounting="allocations", actor="test", reason="enable")
    _, _, first = await _setup(space, group="a", start=9001)
    _, _, second = await _setup(space, group="b", start=9002)
    await _expire(hub_under_test, first + second, backend_sessionmaker)
    foreign = hub_under_test.space("foreign-recovery", create=True)
    assert await foreign.maintenance.cleanup_proxy_allocations() == 0
    assert await space.maintenance.cleanup_proxy_allocations(group="b") == 1
    assert await _state(space, first[0]) == "reserved" and await _state(space, second[0]) == "released"
    assert await space.maintenance.cleanup_proxy_allocations(group="a") == 1


async def test_orphan_closed_after_old_runtime_lost_current_row(space, hub_under_test, backend_sessionmaker):
    _, _, (old,) = await _setup(space)
    if backend_sessionmaker is None:
        store = hub_under_test._store
        store._state.leases.pop(old.identity_id)
        store._state.history[old.lease_id] = replace(
            store._state.history[old.lease_id], released_at=hub_under_test.clock.now()
        )
    else:
        async with backend_sessionmaker.begin() as session:
            await session.execute(delete(LeaseRecord).where(LeaseRecord.lease_id == old.lease_id))
            await session.execute(
                update(LeaseHistoryRecord)
                .where(LeaseHistoryRecord.lease_id == old.lease_id)
                .values(released_at=func.now(), end_reason="expired")
            )
    assert await space.maintenance.cleanup_proxy_allocations() == 1
    assert await _state(space, old) == "released"


async def test_cursor_does_not_advance_after_rollback(
    space, hub_under_test, backend_sessionmaker, monkeypatch
):
    proxy, _, (old,) = await _setup(space)
    await _expire(hub_under_test, [old], backend_sessionmaker)
    async with space._tx() as tx:
        backend = type(tx)
    original = backend.proxy_set_counters
    failed = False

    async def fail(tx, proxy_id, **counts):
        nonlocal failed
        await original(tx, proxy_id, **counts)
        if not failed:
            failed = True
            raise RuntimeError("after counter write")

    monkeypatch.setattr(backend, "proxy_set_counters", fail)
    with pytest.raises(RuntimeError, match="after counter"):
        await space.maintenance.cleanup_proxy_allocations(limit=1)
    assert await _state(space, old) == "reserved"
    assert not hub_under_test._proxy_recovery._cursors
    async with space._tx() as tx:
        assert (await tx.get_proxy(proxy.id)).reserved_count == 1
    assert await space.maintenance.cleanup_proxy_allocations(limit=1) == 1


async def test_fixed_cycle_upper_bound_and_restart(space, hub_under_test, backend_sessionmaker):
    _, _, leases = await _setup(space, count=3)
    await _expire(hub_under_test, leases, backend_sessionmaker)
    assert await space.maintenance.cleanup_proxy_allocations(limit=1) == 1
    _, _, fresh = await _setup(space, group="new", start=9003)
    await _expire(hub_under_test, fresh, backend_sessionmaker)
    assert await space.maintenance.cleanup_proxy_allocations(limit=1) == 1
    assert await space.maintenance.cleanup_proxy_allocations(limit=1) == 1
    cursor, _ = next(iter(hub_under_test._proxy_recovery._cursors.values()))
    assert cursor.until is None and await _state(space, fresh[0]) == "reserved"
    assert await ProxyRecovery(hub_under_test).run(space, limit=1) == 1


async def test_retention_respects_history_and_longer_key_ttl_not_closed_pointer(space, hub_under_test):
    _, _, (lease,) = await _setup(space)
    original = Lease(lease.pool, lease._row, lease.identity, lease.proxy_info, lease._proxy_allocation)
    await lease.release()
    store = hub_under_test._store
    retention = {"lease_history": timedelta(days=-2), "operation_keys": timedelta(days=-1)}
    assert (await store.cleanup_maintenance(retention, frozenset({"proxy_allocations"}))) == {
        "proxy_allocations": 0
    }
    await store.cleanup_maintenance(retention, frozenset({"lease_history"}))
    keep_keys = {**retention, "operation_keys": timedelta(days=1)}
    assert (await store.cleanup_maintenance(keep_keys, frozenset({"proxy_allocations"}))) == {
        "proxy_allocations": 0
    }
    assert (await store.cleanup_maintenance(retention, frozenset({"proxy_allocations"}))) == {
        "proxy_allocations": 1
    }
    assert await _state(space, lease) is None
    with pytest.raises((LeaseLost, LeaseOwnershipLost)):
        await original.confirm_proxy(reason="old expired proof")


async def test_lazy_counter_repair_recovers_full_proxy_without_pending_ledger(space):
    proxy, pool, (old,) = await _setup(space)
    await old.release()
    async with space._tx() as tx:
        await tx.proxy_set_counters(proxy.id, bound=0, reserved=1, draining=0)
    other = (
        await register(
            space,
            service="fake_tg",
            external_ref="counter-repair",
            lifecycle="ready",
            session={"auth_key": "b"},
        )
    ).identity_id
    lease = await pool.acquire(among=[other])
    assert lease.proxy_id == proxy.id
    async with space._tx() as tx:
        assert (await tx.get_proxy(proxy.id)).reserved_count == 1
        assert len(await tx.proxy_allocations(proxy_id=proxy.id)) == 1
    await lease.release()


async def test_counter_repair_is_bounded_and_ignores_legacy(space, hub_under_test):
    proxy, _, (lease,) = await _setup(space)
    await lease.release()
    legacy = await space.proxies.create("http://127.0.0.1:9020", group="legacy")
    async with space._tx() as tx:
        await tx.proxy_set_counters(proxy.id, bound=0, reserved=1, draining=0)
        await tx.proxy_set_counters(legacy.id, bound=1, reserved=0, draining=0)
    recovery = hub_under_test._proxy_recovery
    assert await recovery.repair_counters(space, limit=1) == 1
    assert await recovery.repair_counters(space, limit=1) == 0
    async with space._tx() as tx:
        assert (await tx.get_proxy(proxy.id)).reserved_count == 0
        assert (await tx.get_proxy(legacy.id)).bound_count == 1


async def test_counter_repair_rollback_revisits_same_page(space, hub_under_test, monkeypatch):
    proxy, _, (lease,) = await _setup(space)
    await lease.release()
    async with space._tx() as tx:
        backend = type(tx)
        await tx.proxy_set_counters(proxy.id, bound=0, reserved=1, draining=0)
    original = backend.proxy_set_counters

    async def fail(tx, proxy_id, **counts):
        await original(tx, proxy_id, **counts)
        raise RuntimeError("counter repair rollback")

    with monkeypatch.context() as patch:
        patch.setattr(backend, "proxy_set_counters", fail)
        with pytest.raises(RuntimeError, match="counter repair rollback"):
            await hub_under_test._proxy_recovery.repair_counters(space, limit=1)
    assert not hub_under_test._proxy_recovery._counter_cursors
    async with space._tx() as tx:
        assert (await tx.get_proxy(proxy.id)).reserved_count == 1
    assert await hub_under_test._proxy_recovery.repair_counters(space, limit=1) == 1


async def test_counter_repair_precedes_close_when_count_is_too_low(
    space, hub_under_test, backend_sessionmaker
):
    proxy, _, (lease,) = await _setup(space)
    await _expire(hub_under_test, [lease], backend_sessionmaker)
    async with space._tx() as tx:
        await tx.proxy_set_counters(proxy.id, bound=0, reserved=0, draining=0)
    assert await space.maintenance.cleanup_proxy_allocations(limit=1) == 1
    async with space._tx() as tx:
        assert (await tx.get_proxy(proxy.id)).reserved_count == 0


@pytest.mark.parametrize("limit", [0, -1, True, 1001])
async def test_cleanup_limit_validation(space, limit):
    with pytest.raises(ValidationFailed):
        await space.maintenance.cleanup_proxy_allocations(limit=limit)
