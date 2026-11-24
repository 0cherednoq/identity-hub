"""PA1.2: единый ledger, ёмкость и поколения на обоих Store."""

import asyncio
from uuid import uuid4

import pytest
from tests.fixtures.api import register

from identity_hub import (
    IdentityHubError,
    LeaseOwnershipLost,
    LeaseUnavailable,
    ProxyAssignmentConflict,
    ProxyPolicy,
    SpaceHandle,
    ValidationFailed,
)
from identity_hub.proxy_allocator import attach_use, close_use, confirm, detach, reserve
from identity_hub.store.base import LeaseCounters, LeaseFence

pytestmark = pytest.mark.conformance


@pytest.mark.parametrize(
    ("name", "accounting"), [("", "allocations"), ("g", "unknown"), ([], "bindings"), ("g", [])]
)
async def test_invalid_configuration_does_not_create_space(space: SpaceHandle, name, accounting) -> None:
    before = await space.hub._store.list_spaces()
    with pytest.raises(ValidationFailed):
        await space.proxies.configure_group(
            name, capacity_accounting=accounting, actor="test", reason="invalid"
        )
    assert await space.hub._store.list_spaces() == before


async def _account(space: SpaceHandle, ref: str):
    return (
        await register(
            space, service="fake_tg", external_ref=ref, lifecycle="ready", session={"auth_key": "k"}
        )
    ).identity_id


async def test_group_configuration_is_idempotent_and_converts_legacy_binding(space: SpaceHandle) -> None:
    proxy = await space.proxies.create("http://127.0.0.1:8901", group="g", max_identities=1)
    identity = await _account(space, "a")
    await space.proxies.rebind(identity, proxy.id, reason="legacy", actor="test")
    group = await space.proxies.configure_group(
        "g", capacity_accounting="allocations", actor="test", reason="managed"
    )
    assert group.capacity_accounting == "allocations"
    assert (
        await space.proxies.configure_group(
            "g", capacity_accounting="allocations", actor="test", reason="repeat"
        )
        == group
    )
    async with space._tx() as tx:
        allocations = await tx.proxy_allocations(identity_id=identity)
        assert len(allocations) == 1 and allocations[0].state == "bound"
        assert (await tx.proxy_bindings(identity_id=identity))[0].allocation_id == allocations[0].id
    assert (await space.proxies.list(group="g")).items[0].bound_count == 1
    with pytest.raises(ValidationFailed):
        await space.proxies.configure_group(
            "g", capacity_accounting="bindings", actor="test", reason="unsafe"
        )


async def test_configuration_requires_drain_before_enabling(space: SpaceHandle) -> None:
    await space.proxies.create("http://127.0.0.1:8902", group="g")
    identity = await _account(space, "a")
    pool = space.pools.define("p", service="fake_tg")
    async with pool.lease(among=[identity]):
        with pytest.raises(ValidationFailed, match="drain"):
            await space.proxies.configure_group(
                "g", capacity_accounting="allocations", actor="test", reason="switch"
            )
    assert (await space.proxies.get_group("g")).capacity_accounting == "bindings"


async def test_two_reservations_cannot_take_last_slot(space: SpaceHandle) -> None:
    proxy = await space.proxies.create("http://127.0.0.1:8903", group="g", max_identities=1)
    await space.proxies.configure_group("g", capacity_accounting="allocations", actor="test", reason="switch")
    identities = [await _account(space, ref) for ref in ("a", "b")]
    pool = space.pools.define("p", service="fake_tg")
    leases = [await pool.acquire(among=[identity]) for identity in identities]

    async def attempt(index: int):
        async with space._tx() as tx:
            return await reserve(tx, identities[index], "g", owner_lease_id=leases[index].lease_id)

    results = await asyncio.gather(attempt(0), attempt(1), return_exceptions=True)
    assert sum(isinstance(result, LeaseUnavailable) for result in results) == 1
    async with space._tx() as tx:
        assert len(await tx.proxy_allocations(proxy_id=proxy.id)) == 1
        row = await tx.get_proxy(proxy.id)
        assert row is not None and row.reserved_count == 1 and row.bound_count == 0
    for lease in leases:
        await lease.release()


async def test_rollback_returns_capacity_and_confirm_does_not_need_extra_slot(space: SpaceHandle) -> None:
    proxy = await space.proxies.create("http://127.0.0.1:8904", group="g", max_identities=1)
    await space.proxies.configure_group("g", capacity_accounting="allocations", actor="test", reason="switch")
    identity = await _account(space, "a")
    lease = await space.pools.define("p", service="fake_tg").acquire(among=[identity])
    with pytest.raises(RuntimeError, match="rollback"):
        async with space._tx() as tx:
            await reserve(tx, identity, "g", owner_lease_id=lease.lease_id)
            raise RuntimeError("rollback")
    async with space._tx() as tx:
        assert await tx.proxy_allocations(identity_id=identity) == []
        allocation = await reserve(tx, identity, "g", owner_lease_id=lease.lease_id)
        await attach_use(tx, lease.fence, allocation.id)
        result, changed = await confirm(tx, lease.fence, allocation.id)
        assert result.state == "bound" and changed
        _, changed = await confirm(tx, lease.fence, allocation.id)
        assert not changed
        row = await tx.get_proxy(proxy.id)
        assert row is not None and (row.bound_count, row.reserved_count) == (1, 0)
    await lease.release()


async def test_detach_drains_active_use_and_cleanup_is_idempotent(space: SpaceHandle) -> None:
    proxy = await space.proxies.create("http://127.0.0.1:8905", group="g", max_identities=1)
    await space.proxies.configure_group("g", capacity_accounting="allocations", actor="test", reason="switch")
    identity = await _account(space, "a")
    lease = await space.pools.define("p", service="fake_tg").acquire(among=[identity])
    async with space._tx() as tx:
        allocation = await reserve(tx, identity, "g", owner_lease_id=lease.lease_id)
        await attach_use(tx, lease.fence, allocation.id)
        await confirm(tx, lease.fence, allocation.id)
        detached = await detach(tx, identity, expected_binding_id=allocation.id)
        assert detached is not None and detached.state == "draining"
        row = await tx.get_proxy(proxy.id)
        assert row is not None and (row.bound_count, row.draining_count) == (0, 1)
        assert await tx.proxy_bindings(identity_id=identity) == []
        assert not await close_use(tx, allocation.id)
    await lease.release()
    async with space._tx() as tx:
        assert (await tx.proxy_allocation(allocation.id)).state == "released"
        assert not await close_use(tx, allocation.id)
        row = await tx.get_proxy(proxy.id)
        assert row is not None and (row.bound_count, row.reserved_count, row.draining_count) == (0, 0, 0)


async def test_late_commands_cannot_detach_replacement(space: SpaceHandle) -> None:
    await space.proxies.create_many(
        ["http://127.0.0.1:8910", "http://127.0.0.1:8911"], group="g", max_identities=1
    )
    await space.proxies.configure_group("g", capacity_accounting="allocations", actor="test", reason="switch")
    identity = await _account(space, "a")
    pool = space.pools.define("p", service="fake_tg")
    first = await pool.acquire(among=[identity])
    async with space._tx() as tx:
        old = await reserve(tx, identity, "g", owner_lease_id=first.lease_id)
        await attach_use(tx, first.fence, old.id)
        await confirm(tx, first.fence, old.id)
    await first.release()
    second = await pool.acquire(among=[identity])
    async with space._tx() as tx:
        new = await reserve(tx, identity, "g", owner_lease_id=second.lease_id)
        assert new.expected_binding_id == old.id and new.proxy_id != old.proxy_id
        await attach_use(tx, second.fence, new.id)
        await confirm(tx, second.fence, new.id)
    with pytest.raises(ProxyAssignmentConflict, match="assignment_changed"):
        async with space._tx() as tx:
            await detach(tx, identity, expected_binding_id=old.id)
    with pytest.raises(LeaseOwnershipLost):
        async with space._tx() as tx:
            await confirm(tx, first.fence, old.id)
    async with space._tx() as tx:
        assert (await tx.proxy_bindings(identity_id=identity))[0].allocation_id == new.id
        assert (await tx.proxy_allocation(old.id)).state == "released"
    await second.release()


async def test_foreign_owner_cannot_commit_reservation(space: SpaceHandle) -> None:
    proxy = await space.proxies.create("http://127.0.0.1:8912", group="g", max_identities=1)
    await space.proxies.configure_group("g", capacity_accounting="allocations", actor="test", reason="switch")
    identities = [await _account(space, ref) for ref in ("a", "b")]
    lease = await space.pools.define("p", service="fake_tg").acquire(among=[identities[0]])
    with pytest.raises(IdentityHubError):
        async with space._tx() as tx:
            await reserve(tx, identities[1], "g", owner_lease_id=lease.lease_id)
    async with space._tx() as tx:
        assert await tx.proxy_allocations(identity_id=identities[1]) == []
        assert (await tx.get_proxy(proxy.id)).reserved_count == 0
    await lease.release()


async def test_strict_groups_cannot_fall_through_to_legacy_writes(space: SpaceHandle) -> None:
    proxy = await space.proxies.create("http://127.0.0.1:8913", group="g")
    await space.proxies.configure_group("g", capacity_accounting="allocations", actor="test", reason="switch")
    identity = await _account(space, "a")
    pool = space.pools.define("p", service="fake_tg", proxy_policy=ProxyPolicy(group="g"))
    async with pool.lease(among=[identity]) as lease:
        assert lease.proxy_allocation_id is not None and lease.proxy_assignment_state == "bound"
    with pytest.raises(ValidationFailed, match="allocator"):
        async with space._tx() as tx:
            await tx.bind_proxy(identity, proxy.id, None)
    with pytest.raises(ValidationFailed, match="accounting modes"):
        await space.proxies.move(proxy.id, group="legacy", actor="test")
    async with space._tx() as tx:
        assert await tx.current_lease(identity) is None
        assert (await tx.proxy_allocations(identity_id=identity))[0].state == "bound"
        assert (await tx.get_proxy(proxy.id)).bound_count == 1


async def test_conversion_rejects_existing_physical_overcommit(space: SpaceHandle) -> None:
    proxy = await space.proxies.create("http://127.0.0.1:8914", group="g", max_identities=1)
    identities = [await _account(space, ref) for ref in ("a", "b")]
    pool = space.pools.define("p", service="fake_tg", proxy_policy=ProxyPolicy(group="g", max_identities=2))
    for identity in identities:
        async with pool.lease(among=[identity]):
            pass
    with pytest.raises(ValidationFailed, match="physical"):
        await space.proxies.configure_group(
            "g", capacity_accounting="allocations", actor="test", reason="switch"
        )
    assert (await space.proxies.get_group("g")).capacity_accounting == "bindings"
    async with space._tx() as tx:
        assert await tx.proxy_allocations(proxy_id=proxy.id) == []
        assert (await tx.get_proxy(proxy.id)).bound_count == 2


async def test_reservation_can_precede_owner_creation_in_one_transaction(space: SpaceHandle) -> None:
    await space.proxies.create("http://127.0.0.1:8915", group="g", max_identities=1)
    await space.proxies.configure_group("g", capacity_accounting="allocations", actor="test", reason="switch")
    identity = await _account(space, "a")
    prospective = uuid4()
    async with space._tx() as tx:
        allocation = await reserve(tx, identity, "g", owner_lease_id=prospective)
        row, _ = await tx.acquire_lease(
            await tx.get_identity(identity),
            pool="internal",
            holder="test",
            ttl_seconds=60,
            idempotency_key=None,
            shared=False,
            proxy_id=allocation.proxy_id,
            lease_id=prospective,
        )
        fence = LeaseFence(identity, row.lease_id, row.token, row.fencing)
        await attach_use(tx, fence, allocation.id)
    assert row.lease_id == prospective
    async with space._tx() as tx:
        assert (await tx.current_lease(identity)).proxy_allocation_id == allocation.id
        assert await close_use(tx, allocation.id, finishing_lease_id=row.lease_id)
        await tx.release_lease(row, "released", LeaseCounters(operations={}), shared=False)


async def test_missing_deferred_owner_rolls_back_at_commit(space: SpaceHandle) -> None:
    proxy = await space.proxies.create("http://127.0.0.1:8916", group="g", max_identities=1)
    await space.proxies.configure_group("g", capacity_accounting="allocations", actor="test", reason="switch")
    identity = await _account(space, "a")
    reserved = False
    with pytest.raises(IdentityHubError):
        async with space._tx() as tx:
            await reserve(tx, identity, "g", owner_lease_id=uuid4())
            reserved = True
    assert reserved  # FK отложен до commit, а не проверяется до появления владельца.
    async with space._tx() as tx:
        assert await tx.proxy_allocations(identity_id=identity) == []
        assert (await tx.get_proxy(proxy.id)).reserved_count == 0


async def test_public_unbind_allows_drained_group_to_return_to_legacy(space: SpaceHandle) -> None:
    proxy = await space.proxies.create("http://127.0.0.1:8917", group="g", max_identities=1)
    identity = await _account(space, "a")
    await space.proxies.rebind(identity, proxy.id, actor="test", reason="legacy")
    await space.proxies.configure_group("g", capacity_accounting="allocations", actor="test", reason="switch")
    async with space._tx() as tx:
        allocation = (await tx.proxy_allocations(identity_id=identity))[0]
    assert await space.proxies.unbind(
        identity, expected_binding_id=allocation.id, actor="test", reason="remove"
    )
    assert not await space.proxies.unbind(
        identity, expected_binding_id=allocation.id, actor="test", reason="repeat"
    )
    async with space._tx() as tx:
        assert (await tx.proxy_allocation(allocation.id)).state == "released"
        assert (await tx.get_proxy(proxy.id)).bound_count == 0
    await space.proxies.configure_group("g", capacity_accounting="bindings", actor="test", reason="rollback")
    pool = space.pools.define("p", service="fake_tg", proxy_policy=ProxyPolicy(group="g"))
    async with pool.lease(among=[identity]) as lease:
        assert lease.proxy_id == proxy.id


async def test_public_unbind_keeps_active_use_occupied(space: SpaceHandle) -> None:
    proxy = await space.proxies.create("http://127.0.0.1:8918", group="g", max_identities=1)
    await space.proxies.configure_group("g", capacity_accounting="allocations", actor="test", reason="switch")
    identity = await _account(space, "a")
    lease = await space.pools.define("p", service="fake_tg").acquire(among=[identity])
    async with space._tx() as tx:
        allocation = await reserve(tx, identity, "g", owner_lease_id=lease.lease_id)
        await attach_use(tx, lease.fence, allocation.id)
        await confirm(tx, lease.fence, allocation.id)
    with pytest.raises(ProxyAssignmentConflict):
        await space.proxies.unbind(identity, expected_binding_id=uuid4(), actor="test", reason="stale")
    assert await space.proxies.unbind(
        identity, expected_binding_id=allocation.id, actor="test", reason="detach"
    )
    async with space._tx() as tx:
        assert await tx.proxy_bindings(identity_id=identity) == []
        assert (await tx.proxy_allocation(allocation.id)).state == "draining"
        row = await tx.get_proxy(proxy.id)
        assert (row.bound_count, row.reserved_count, row.draining_count) == (0, 0, 1)
    await lease.release()
    async with space._tx() as tx:
        assert (await tx.proxy_allocation(allocation.id)).state == "released"
        assert not await close_use(tx, allocation.id)


async def test_stale_public_unbind_does_not_cancel_new_reservation(space: SpaceHandle) -> None:
    await space.proxies.create("http://127.0.0.1:8919", group="g", max_identities=1)
    await space.proxies.configure_group("g", capacity_accounting="allocations", actor="test", reason="switch")
    identity = await _account(space, "a")
    lease = await space.pools.define("p", service="fake_tg").acquire(among=[identity])
    async with space._tx() as tx:
        allocation = await reserve(tx, identity, "g", owner_lease_id=lease.lease_id)
    assert not await space.proxies.unbind(
        identity, expected_binding_id=uuid4(), actor="test", reason="late old generation"
    )
    async with space._tx() as tx:
        assert (await tx.proxy_allocation(allocation.id)).state == "reserved"
    assert await space.proxies.unbind(identity, actor="test", reason="cancel current reservation")
    async with space._tx() as tx:
        assert (await tx.proxy_allocation(allocation.id)).state == "draining"
    await lease.release()
