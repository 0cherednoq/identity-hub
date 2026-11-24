"""PA1.3: удаление ресурса не уничтожает историю и не обходит drain."""

import pytest
from tests.fixtures.api import register

from identity_hub import LeaseOwnershipLost, SpaceHandle, ValidationFailed
from identity_hub.proxy_allocator import attach_use, confirm, reserve

pytestmark = pytest.mark.conformance


async def _setup(space: SpaceHandle):
    proxy = await space.proxies.create("http://127.0.0.1:8940", group="g", max_identities=1)
    await space.proxies.configure_group(
        "g", capacity_accounting="allocations", actor="admin", reason="switch"
    )
    account = await register(
        space, service="fake_tg", external_ref="a", lifecycle="ready", session={"auth_key": "k"}
    )
    return proxy, account.identity_id


@pytest.mark.parametrize("state", ["reserved", "bound", "draining"])
async def test_proxy_delete_requires_drain_then_preserves_tombstone(space: SpaceHandle, state: str) -> None:
    proxy, identity = await _setup(space)
    lease = await space.pools.define("p", service="fake_tg").acquire(among=[identity])
    async with space._tx() as tx:
        allocation = await reserve(tx, identity, "g", owner_lease_id=lease.lease_id)
        await attach_use(tx, lease.fence, allocation.id)
        if state == "bound":
            await confirm(tx, lease.fence, allocation.id)
    if state == "draining":
        await space.proxies.unbind(identity, actor="admin", reason="cancel")
    with pytest.raises(ValidationFailed, match="drain"):
        await space.proxies.delete(proxy.id, unbind=True, actor="admin")
    async with space._tx() as tx:
        assert (await tx.proxy_allocation(allocation.id)).state == state
        assert await tx.get_proxy(proxy.id) is not None
    await lease.release()
    assert await space.proxies.delete(proxy.id, unbind=True, actor="admin") == (1 if state == "bound" else 0)
    async with space._tx() as tx:
        assert await tx.get_proxy(proxy.id) is None
        assert await tx.get_identity(identity) is not None
        row = await tx.proxy_allocation(allocation.id)
        assert row.state == "released" and row.owner_lease_id is None
        assert (row.identity_id, row.proxy_id) == (identity, proxy.id)


async def test_identity_delete_revoke_closes_use_and_keeps_ledger(space: SpaceHandle) -> None:
    proxy, identity = await _setup(space)
    lease = await space.pools.define("p", service="fake_tg").acquire(among=[identity])
    async with space._tx() as tx:
        allocation = await reserve(tx, identity, "g", owner_lease_id=lease.lease_id)
        await attach_use(tx, lease.fence, allocation.id)
        await confirm(tx, lease.fence, allocation.id)
    with pytest.raises(ValidationFailed):
        await space.identities.delete(identity, actor="admin")
    await space.identities.delete(identity, revoke=True, actor="admin")
    async with space._tx() as tx:
        assert await tx.get_identity(identity) is None
        assert (await tx.proxy_allocation(allocation.id)).state == "released"
        row = await tx.get_proxy(proxy.id)
        assert (row.bound_count, row.reserved_count, row.draining_count) == (0, 0, 0)
        with pytest.raises(LeaseOwnershipLost):
            await tx.check_fence(lease.fence)
    assert await space.proxies.delete(proxy.id, actor="admin") == 0
    async with space._tx() as tx:
        assert (await tx.proxy_allocation(allocation.id)).state == "released"


async def test_atomic_delete_many_rolls_back_resource_and_assignment(space: SpaceHandle) -> None:
    first, identity = await _setup(space)
    second = await space.proxies.create("http://127.0.0.1:8941", group="g", max_identities=1)
    cold, busy = sorted((first, second), key=lambda proxy: proxy.id)
    await space.proxies.rebind(identity, cold.id, actor="admin", reason="cold")
    owner = await register(
        space, service="fake_tg", external_ref="b", lifecycle="ready", session={"auth_key": "k"}
    )
    lease = await space.pools.define("p", service="fake_tg").acquire(among=[owner.identity_id])
    async with space._tx() as tx:
        cold_binding = (await tx.proxy_bindings(identity_id=identity))[0].allocation_id
        active = await reserve(tx, owner.identity_id, "g", owner_lease_id=lease.lease_id, proxy_id=busy.id)
        await attach_use(tx, lease.fence, active.id)
    with pytest.raises(ValidationFailed, match="drain"):
        await space.proxies.delete_many([cold.id, busy.id], unbind=True, atomic=True, actor="admin")
    async with space._tx() as tx:
        assert await tx.get_proxy(cold.id) is not None and await tx.get_proxy(busy.id) is not None
        assert (await tx.proxy_allocation(cold_binding)).state == "bound"
        assert (await tx.get_proxy(cold.id)).bound_count == 1
        assert (await tx.get_proxy(busy.id)).reserved_count == 1
    await lease.release()


async def test_delete_many_keeps_busy_identity_and_releases_cold_assignment(space: SpaceHandle) -> None:
    proxy, cold = await _setup(space)
    await space.proxies.rebind(cold, proxy.id, actor="admin", reason="cold")
    owner = await register(
        space, service="fake_tg", external_ref="b", lifecycle="ready", session={"auth_key": "k"}
    )
    lease = await space.pools.define("p", service="fake_tg").acquire(among=[owner.identity_id])
    result = await space.identities.delete_many([cold, owner.identity_id], actor="admin")
    assert result.applied == [cold] and [item.key for item in result.rejected] == [owner.identity_id]
    async with space._tx() as tx:
        assert await tx.get_identity(cold) is None
        assert await tx.get_identity(owner.identity_id) is not None
        assert (await tx.get_proxy(proxy.id)).bound_count == 0
        assert len(await tx.proxy_allocations(identity_id=cold, include_released=True)) == 1
    await lease.release()
