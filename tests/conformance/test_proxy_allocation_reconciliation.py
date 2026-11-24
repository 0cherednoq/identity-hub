"""Общий doctor восстанавливает три счётчика по ledger, сохраняя живую занятость."""

import pytest
from tests.fixtures.api import register

from identity_hub import SpaceHandle
from identity_hub.proxy_allocator import attach_use, reserve

pytestmark = pytest.mark.conformance


async def test_doctor_repairs_all_allocation_counters_without_freeing_uses(space: SpaceHandle) -> None:
    proxy = await space.proxies.create("http://127.0.0.1:8943", group="g", max_identities=3)
    await space.proxies.configure_group("g", capacity_accounting="allocations", actor="ops", reason="enable")
    identities = [
        (
            await register(
                space, service="fake_tg", external_ref=ref, lifecycle="ready", session={"auth_key": "k"}
            )
        ).identity_id
        for ref in ("bound", "reserved", "draining")
    ]
    await space.proxies.rebind(identities[0], proxy.id, actor="admin", reason="trusted")
    pool = space.pools.define("p", service="fake_tg")
    leases = [await pool.acquire(among=[identity]) for identity in identities[1:]]
    async with space._tx() as tx:
        for identity, lease in zip(identities[1:], leases, strict=True):
            allocation = await reserve(tx, identity, "g", owner_lease_id=lease.lease_id)
            await attach_use(tx, lease.fence, allocation.id)
    await space.proxies.unbind(identities[2], actor="admin", reason="cancel")
    async with space._tx() as tx:
        await tx.proxy_set_counters(proxy.id, bound=0, reserved=0, draining=0)
    issues = await space.hub.doctor.check(fix=True)
    assert {
        issue.kind: (issue.expected, issue.actual) for issue in issues if issue.entity_id == proxy.id
    } == {"proxy.bound_count": (1, 0), "proxy.reserved_count": (1, 0), "proxy.draining_count": (1, 0)}
    async with space._tx() as tx:
        row = await tx.get_proxy(proxy.id)
        assert (row.bound_count, row.reserved_count, row.draining_count) == (1, 1, 1)
    assert not [issue for issue in await space.hub.doctor.check() if issue.entity_id == proxy.id]
    for lease in leases:
        await lease.release()
