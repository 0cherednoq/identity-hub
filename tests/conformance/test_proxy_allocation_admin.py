"""PA1.3: административные команды сохраняют поколения и физическую ёмкость."""

from dataclasses import replace
from uuid import uuid4

import pytest
from tests.fixtures.api import register

from identity_hub import (
    ImportRecord,
    LeaseUnavailable,
    ProxyAssignmentConflict,
    ProxyBindingPolicy,
    ProxyPolicy,
    SpaceHandle,
    ValidationFailed,
)
from identity_hub.proxy_allocator import attach_use, close_use, confirm, reserve

pytestmark = pytest.mark.conformance


async def _account(space: SpaceHandle, ref: str):
    return (
        await register(
            space, service="fake_tg", external_ref=ref, lifecycle="ready", session={"auth_key": "k"}
        )
    ).identity_id


async def _group(space: SpaceHandle):
    first = await space.proxies.create("http://127.0.0.1:8921", group="g", max_identities=1)
    second = await space.proxies.create("http://127.0.0.1:8922", group="g", max_identities=1)
    await space.proxies.configure_group("g", capacity_accounting="allocations", actor="test", reason="switch")
    return first, second


async def test_admin_rebind_is_idempotent_and_generation_checked(space: SpaceHandle) -> None:
    first, second = await _group(space)
    identity = await _account(space, "a")
    assert await space.proxies.rebind(identity, first.id, actor="admin", reason="trusted")
    async with space._tx() as tx:
        old = (await tx.proxy_bindings(identity_id=identity))[0].allocation_id
        assert (await tx.proxy_allocation(old)).reason == "administrative_rebind"
    assert not await space.proxies.rebind(
        identity, first.id, expected_binding_id=old, actor="admin", reason="repeat"
    )
    assert await space.proxies.rebind(
        identity, second.id, expected_binding_id=old, actor="admin", reason="replace"
    )
    with pytest.raises(ProxyAssignmentConflict):
        await space.proxies.rebind(identity, first.id, expected_binding_id=old, actor="admin", reason="late")
    with pytest.raises(ProxyAssignmentConflict):
        await space.proxies.unbind(identity, expected_binding_id=old, actor="admin", reason="late")
    async with space._tx() as tx:
        binding = (await tx.proxy_bindings(identity_id=identity))[0]
        assert binding.proxy_id == second.id and binding.allocation_id != old
        assert (await tx.proxy_allocation(old)).state == "released"
        assert (await tx.get_proxy(first.id)).bound_count == 0
        assert (await tx.get_proxy(second.id)).bound_count == 1


async def test_admin_rebind_rejects_full_target_without_losing_old_binding(space: SpaceHandle) -> None:
    first, second = await _group(space)
    identities = [await _account(space, ref) for ref in ("a", "b")]
    await space.proxies.rebind(identities[0], first.id, actor="admin", reason="old")
    await space.proxies.rebind(identities[1], second.id, actor="admin", reason="occupy")
    with pytest.raises(LeaseUnavailable):
        await space.proxies.rebind(identities[0], second.id, actor="admin", reason="overcommit")
    async with space._tx() as tx:
        assert (await tx.proxy_bindings(identity_id=identities[0]))[0].proxy_id == first.id
        assert (await tx.get_proxy(first.id)).bound_count == 1
        assert (await tx.get_proxy(second.id)).bound_count == 1


async def test_admin_rebind_retains_old_active_use(space: SpaceHandle) -> None:
    first, second = await _group(space)
    identity = await _account(space, "a")
    lease = await space.pools.define("p", service="fake_tg").acquire(among=[identity])
    async with space._tx() as tx:
        old = await reserve(tx, identity, "g", owner_lease_id=lease.lease_id, proxy_id=first.id)
        await attach_use(tx, lease.fence, old.id)
        await confirm(tx, lease.fence, old.id)
    await space.proxies.rebind(
        identity, second.id, expected_binding_id=old.id, actor="admin", reason="replace"
    )
    with pytest.raises(ProxyAssignmentConflict):
        async with space._tx() as tx:
            await confirm(tx, lease.fence, old.id)
    async with space._tx() as tx:
        assert (await tx.proxy_allocation(old.id)).state == "draining"
        row = await tx.get_proxy(first.id)
        assert (row.bound_count, row.draining_count) == (0, 1)
        assert (await tx.get_proxy(second.id)).bound_count == 1
    await lease.release()
    async with space._tx() as tx:
        assert (await tx.proxy_allocation(old.id)).state == "released"
        assert not await close_use(tx, old.id)
        assert (await tx.get_proxy(first.id)).draining_count == 0


async def test_rebind_many_uses_same_capacity_and_savepoint_rollback(space: SpaceHandle) -> None:
    first, _ = await _group(space)
    identities = sorted([await _account(space, ref) for ref in ("a", "b")])
    result = await space.proxies.rebind_many(
        dict.fromkeys(identities, first.id), actor="admin", reason="import trusted accounts"
    )
    assert result.applied == [identities[0]]
    assert [(item.key, item.code) for item in result.rejected] == [(identities[1], "no_resource_available")]
    async with space._tx() as tx:
        assert (await tx.get_proxy(first.id)).bound_count == 1
        assert await tx.proxy_allocations(identity_id=identities[1]) == []


async def test_legacy_binding_with_pending_replacement_is_unbound_once(space: SpaceHandle) -> None:
    managed, _ = await _group(space)
    legacy = await space.proxies.create("http://127.0.0.1:8923", group="legacy", max_identities=1)
    identity = await _account(space, "a")
    await space.proxies.rebind(identity, legacy.id, actor="admin", reason="old")
    lease = await space.pools.define("p", service="fake_tg").acquire(among=[identity])
    async with space._tx() as tx:
        pending = await reserve(tx, identity, "g", owner_lease_id=lease.lease_id, proxy_id=managed.id)
        await attach_use(tx, lease.fence, pending.id)
    assert await space.proxies.unbind(identity, actor="admin", reason="remove all")
    assert not await space.proxies.unbind(identity, actor="admin", reason="repeat")
    # Теперь прежний legacy proxy можно назначить снова даже при draining нового.
    assert await space.proxies.rebind(identity, legacy.id, actor="admin", reason="trusted again")
    assert await space.proxies.unbind(identity, actor="admin", reason="remove legacy with draining")
    async with space._tx() as tx:
        assert await tx.proxy_bindings(identity_id=identity) == []
        assert (await tx.get_proxy(legacy.id)).bound_count == 0
        assert (await tx.proxy_allocation(pending.id)).state == "draining"
    await lease.release()


async def test_expected_generation_rebind_does_not_create_missing_binding(space: SpaceHandle) -> None:
    first, _ = await _group(space)
    identity = await _account(space, "a")
    with pytest.raises(ProxyAssignmentConflict):
        await space.proxies.rebind(
            identity, first.id, expected_binding_id=uuid4(), actor="admin", reason="late"
        )
    async with space._tx() as tx:
        assert await tx.proxy_allocations(identity_id=identity) == []


async def test_disable_unbind_cancels_reservation_without_removing_other_binding(space: SpaceHandle) -> None:
    first, second = await _group(space)
    identity = await _account(space, "a")
    await space.proxies.rebind(identity, first.id, actor="admin", reason="old")
    lease = await space.pools.define("p", service="fake_tg").acquire(among=[identity])
    async with space._tx() as tx:
        pending = await reserve(tx, identity, "g", owner_lease_id=lease.lease_id, proxy_id=second.id)
        await attach_use(tx, lease.fence, pending.id)
    await space.proxies.disable(second.id, actor="admin", reason="maintenance", unbind=True)
    async with space._tx() as tx:
        assert (await tx.proxy_bindings(identity_id=identity))[0].proxy_id == first.id
        assert (await tx.proxy_allocation(pending.id)).state == "draining"
        row = await tx.get_proxy(second.id)
        assert not row.enabled and (row.bound_count, row.reserved_count, row.draining_count) == (0, 0, 1)
    await lease.release()


async def test_disable_bound_proxy_keeps_active_use_and_cancels_replacement(space: SpaceHandle) -> None:
    first, second = await _group(space)
    identity = await _account(space, "a")
    await space.proxies.rebind(identity, first.id, actor="admin", reason="old")
    lease = await space.pools.define("p", service="fake_tg").acquire(among=[identity])
    async with space._tx() as tx:
        pending = await reserve(tx, identity, "g", owner_lease_id=lease.lease_id, proxy_id=second.id)
        await attach_use(tx, lease.fence, pending.id)
    await space.proxies.disable(first.id, actor="admin", reason="remove", unbind=True)
    async with space._tx() as tx:
        assert await tx.proxy_bindings(identity_id=identity) == []
        assert (await tx.get_proxy(first.id)).bound_count == 0
        assert (await tx.proxy_allocation(pending.id)).state == "draining"
    await lease.release()


async def test_managed_endpoint_changes_require_drain_and_keep_binding_afterwards(space: SpaceHandle) -> None:
    first, _ = await _group(space)
    identity = await _account(space, "a")
    lease = await space.pools.define("p", service="fake_tg").acquire(among=[identity])
    async with space._tx() as tx:
        allocation = await reserve(tx, identity, "g", owner_lease_id=lease.lease_id, proxy_id=first.id)
        await attach_use(tx, lease.fence, allocation.id)
        await confirm(tx, lease.fence, allocation.id)
    with pytest.raises(ValidationFailed, match="drain"):
        await space.proxies.replace_credentials(first.id, username="new", password="pw", actor="admin")
    with pytest.raises(ValidationFailed, match="drain"):
        await space.proxies.update(first.id, host="127.0.0.2", actor="admin")
    await lease.release()
    updated = await space.proxies.update(
        first.id, host="127.0.0.2", username="new", password="pw", actor="admin"
    )
    assert updated.host == "127.0.0.2"
    async with space._tx() as tx:
        assert (await tx.proxy_bindings(identity_id=identity))[0].allocation_id == allocation.id
        assert (await tx.get_proxy(first.id)).bound_count == 1


async def test_managed_move_preserves_ledger_within_same_mode(space: SpaceHandle) -> None:
    first, _ = await _group(space)
    await space.proxies.configure_group(
        "new", capacity_accounting="allocations", actor="test", reason="switch"
    )
    identity = await _account(space, "a")
    await space.proxies.rebind(identity, first.id, actor="admin", reason="trusted")
    moved = await space.proxies.move(first.id, group="new", actor="admin")
    assert moved.group == "new"
    async with space._tx() as tx:
        assert (await tx.get_proxy(first.id)).bound_count == 1
        assert (await tx.proxy_allocations(identity_id=identity))[0].state == "bound"


async def test_deferred_service_import_cannot_implicitly_confirm_proxy(space: SpaceHandle) -> None:
    service = replace(
        space.hub.service("fake_tg"),
        name="deferred_import",
        proxy_policy=ProxyPolicy(group="import", binding=ProxyBindingPolicy()),
    )
    space.hub.register_service(service)
    await space.hub.start()
    report = await space.imports.run(
        service=service.name,
        records=[
            ImportRecord(external_ref="with-proxy", proxy="http://127.0.0.1:8926"),
            ImportRecord(external_ref="without-proxy"),
        ],
        actor="importer",
    )
    assert len(report.created) == 1
    assert len(report.failed) == 1 and report.failed[0][0] == "with-proxy"
    assert "confirmation_required" in report.failed[0][1]
    assert (await space.proxies.list()).items == []
