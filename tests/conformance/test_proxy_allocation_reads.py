"""PA4.2: постоянные пары и физическая занятость — разные публичные показатели."""

import pytest
from tests.conformance.test_proxy_allocation_recovery import _expire, _setup

from identity_hub.read.proxies import ProxyFilters

pytestmark = pytest.mark.conformance


async def test_reads_separate_bound_reserved_draining_and_cleanup(
    space, hub_under_test, backend_sessionmaker
):
    proxy, _, leases = await _setup(space, count=3)
    await leases[0].confirm_proxy(reason="accepted")
    await space.proxies.unbind(leases[1].identity_id, actor="test", reason="cancel reservation")

    listed = (await space.proxies.list()).items[0]
    detail = await space.admin.proxy_detail(proxy.id)
    overview = await space.admin.proxy_overview(filters=ProxyFilters(capacity="full"))
    for row in (listed, detail.proxy, overview.items[0], overview.pools[0]):
        assert (row.bound_count, row.reserved_count, row.draining_count, row.occupied_count) == (1, 1, 1, 3)
        assert row.available_capacity == row.cleanup_pending_count == 0
        assert row.capacity_accounting == "allocations"
    assert len(detail.bound_identities) == 1
    assert not (await space.admin.proxy_overview(filters=ProxyFilters(capacity="available"))).items

    await _expire(hub_under_test, leases, backend_sessionmaker)
    pending = (await space.admin.proxy_detail(proxy.id)).proxy
    assert pending.cleanup_pending_count == 2 and pending.occupied_count == 3
    assert await space.maintenance.cleanup_proxy_allocations() == 2
    clean = (await space.admin.proxy_detail(proxy.id)).proxy
    assert (clean.bound_count, clean.reserved_count, clean.draining_count, clean.occupied_count) == (
        1,
        0,
        0,
        1,
    )
    assert clean.cleanup_pending_count == 0 and clean.available_capacity == 2
    assert (await space.admin.proxy_overview(filters=ProxyFilters(capacity="available"))).items
    assert not (await space.admin.proxy_overview(filters=ProxyFilters(capacity="full"))).items


async def test_reservation_is_unbound_but_capacity_full(space):
    proxy, _, (lease,) = await _setup(space)
    overview = await space.admin.proxy_overview(filters=ProxyFilters(capacity="unbound"))
    assert overview.items[0].id == proxy.id
    assert overview.items[0].bound_count == 0 and overview.items[0].occupied_count == 1
    assert overview.items[0].available_capacity == 0
    await lease.release()


async def test_counts_and_modes_are_scoped_by_space(space, hub_under_test):
    _, _, (lease,) = await _setup(space)
    other = hub_under_test.space("foreign-proxy-reads", create=True)
    legacy = await other.proxies.create("http://127.0.0.1:8888", group="g")
    foreign = (await other.admin.proxy_detail(legacy.id)).proxy
    assert foreign.capacity_accounting == "bindings" and foreign.occupied_count == 0
    assert not (await other.admin.proxy_overview()).items[0].reserved_count
    await lease.release()
