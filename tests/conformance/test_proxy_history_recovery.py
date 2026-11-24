"""PA5/R14: потеря canonical row не оставляет открытый managed use навсегда."""

from dataclasses import replace

import pytest
from sqlalchemy import delete, update
from tests.conformance.test_proxy_allocation_recovery import _expire, _setup

from identity_hub import LeaseOwnershipLost
from identity_hub.postgres.models import LeaseHistoryRecord, LeaseRecord
from identity_hub.read.events import EventFilters

pytestmark = pytest.mark.conformance


async def _lose_current(hub, leases, sessions):
    if sessions is None:
        for lease in leases:
            hub._store._state.leases.pop(lease.identity_id)
    else:
        async with sessions.begin() as session:
            await session.execute(
                delete(LeaseRecord).where(LeaseRecord.lease_id.in_([x.lease_id for x in leases]))
            )


async def _history(space, lease):
    async with space._tx() as tx:
        return next(
            row
            for row in await tx.lease_history(lease.identity_id, limit=20, before=None)
            if row.lease_id == lease.lease_id
        )


async def test_periodic_orphan_cleanup_closes_history_and_keeps_binding(
    space, hub_under_test, backend_sessionmaker
):
    proxy, pool, (lease,) = await _setup(space)
    await lease.confirm_proxy(reason="accepted")
    await _lose_current(hub_under_test, [lease], backend_sessionmaker)
    assert (await _history(space, lease)).released_at is None
    report = await hub_under_test.maintenance._leases(10)
    assert report.counts["proxy_history"] == 1 and report.counts["proxy_allocations"] == 0
    history = await _history(space, lease)
    assert history.released_at is not None and history.end_reason == "lost"
    detail = await space.admin.proxy_detail(proxy.id)
    assert detail.proxy.bound_count == detail.proxy.occupied_count == 1
    assert detail.proxy.in_lease_count == 0 and len(detail.bound_identities) == 1
    assert (await hub_under_test.maintenance._leases(10)).counts["proxy_history"] == 0
    events = await space.admin.event_page(filters=EventFilters(type="lease.lost", include_journal=True))
    assert len(events.items) == 1 and events.items[0].lease_id == lease.lease_id
    assert events.items[0].data["reason"] == "orphaned"
    replacement = await pool.acquire(among=[lease.identity_id])
    assert replacement.proxy_allocation_id == lease.proxy_allocation_id
    with pytest.raises(LeaseOwnershipLost):
        await lease.confirm_proxy(reason="late orphan")
    await replacement.release()


async def test_public_cleanup_closes_orphan_reservation_and_its_history(
    space, hub_under_test, backend_sessionmaker
):
    proxy, _, (lease,) = await _setup(space)
    await _lose_current(hub_under_test, [lease], backend_sessionmaker)
    assert await space.maintenance.cleanup_proxy_allocations(limit=1) == 1
    assert (await _history(space, lease)).end_reason == "lost"
    assert (await space.admin.proxy_detail(proxy.id)).proxy.occupied_count == 0
    assert await space.maintenance.cleanup_proxy_allocations(limit=1) == 0


async def test_even_expired_canonical_generation_is_left_to_normal_expire(
    space, hub_under_test, backend_sessionmaker
):
    _, _, (lease,) = await _setup(space)
    recovery = hub_under_test._proxy_recovery
    assert await recovery.close_orphan_history(space, limit=1) == 0
    await _expire(hub_under_test, [lease], backend_sessionmaker)
    assert await recovery.close_orphan_history(space, limit=1) == 0
    await hub_under_test.maintenance._leases(10)
    assert (await _history(space, lease)).end_reason == "expired"


async def test_previous_history_never_closes_new_live_use_of_same_binding(
    space, hub_under_test, backend_sessionmaker
):
    _, pool, (old,) = await _setup(space)
    await old.confirm_proxy(reason="accepted")
    await old.release()
    current = await pool.acquire(among=[old.identity_id])
    # Исторический дефект: закрытая старая history осталась открытой, новая аренда уже действует.
    if backend_sessionmaker is None:
        state = hub_under_test._store._state
        state.history[old.lease_id] = replace(
            state.history[old.lease_id], released_at=None, duration=None, end_reason=None
        )
    else:
        async with backend_sessionmaker.begin() as session:
            await session.execute(
                update(LeaseHistoryRecord)
                .where(LeaseHistoryRecord.lease_id == old.lease_id)
                .values(released_at=None, end_reason=None)
            )
    assert await hub_under_test._proxy_recovery.close_orphan_history(space, limit=10) == 1
    assert (await _history(space, old)).end_reason == "lost"
    assert (await _history(space, current)).released_at is None
    await current.confirm_proxy(reason="still owned")
    await current.release()


async def test_orphan_history_cursor_is_bounded_and_survives_rollback(
    space, hub_under_test, backend_sessionmaker, monkeypatch
):
    _, _, leases = await _setup(space, count=3)
    for lease in leases:
        await lease.confirm_proxy(reason="accepted")
    await _lose_current(hub_under_test, leases, backend_sessionmaker)
    async with space._tx() as tx:
        backend = type(tx)
    original = backend.proxy_close_orphan_history

    async def fail(tx, rows):
        await original(tx, rows)
        raise RuntimeError("orphan history rollback")

    with monkeypatch.context() as patch:
        patch.setattr(backend, "proxy_close_orphan_history", fail)
        with pytest.raises(RuntimeError, match="orphan history rollback"):
            await hub_under_test._proxy_recovery.close_orphan_history(space, limit=1)
    assert not hub_under_test._proxy_recovery._history_cursors
    assert (await _history(space, leases[0])).released_at is None
    for _ in leases:
        assert await hub_under_test._proxy_recovery.close_orphan_history(space, limit=1) == 1
    assert await hub_under_test._proxy_recovery.close_orphan_history(space, limit=1) == 0


async def test_history_cleanup_respects_space_and_group(space, hub_under_test, backend_sessionmaker):
    await space.proxies.configure_group("b", capacity_accounting="allocations", actor="test", reason="enable")
    _, _, first = await _setup(space, group="a", start=9051)
    _, _, second = await _setup(space, group="b", start=9052)
    for lease in first + second:
        await lease.confirm_proxy(reason="accepted")
    await _lose_current(hub_under_test, first + second, backend_sessionmaker)
    recovery = hub_under_test._proxy_recovery
    assert (
        await recovery.close_orphan_history(hub_under_test.space("foreign-history", create=True), limit=10)
        == 0
    )
    assert await recovery.close_orphan_history(space, limit=10, group="b") == 1
    assert (await _history(space, first[0])).released_at is None
    assert (await _history(space, second[0])).end_reason == "lost"
    assert await recovery.close_orphan_history(space, limit=10, group="a") == 1


async def test_orphan_history_survives_deleted_identity_without_recreating_it(
    space, hub_under_test, backend_sessionmaker
):
    _, _, (old,) = await _setup(space)
    await old.release()
    await space.identities.delete(old.identity_id, actor="test")
    if backend_sessionmaker is None:
        state = hub_under_test._store._state
        state.history[old.lease_id] = replace(
            state.history[old.lease_id], released_at=None, end_reason=None, duration=None
        )
    else:
        async with backend_sessionmaker.begin() as session:
            await session.execute(
                update(LeaseHistoryRecord)
                .where(LeaseHistoryRecord.lease_id == old.lease_id)
                .values(released_at=None, end_reason=None)
            )
    assert await hub_under_test._proxy_recovery.close_orphan_history(space, limit=10) == 1
    events = await space.admin.event_page(filters=EventFilters(type="lease.lost", include_journal=True))
    assert events.items[0].identity_id is None and events.items[0].lease_id == old.lease_id
