"""PA2.1: публичная выдача, владение резервом и воспроизведение исходного результата."""

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from tests.fixtures.api import register
from tests.fixtures.services import FAKE_TG

from identity_hub import (
    IdempotencyConflict,
    LeaseMode,
    LeaseRevoked,
    LeaseUnavailable,
    ProxyBindingPolicy,
    ProxyPolicy,
    SpaceHandle,
    ValidationFailed,
)
from identity_hub.store.base import LeaseCounters

pytestmark = pytest.mark.conformance


async def _setup(space: SpaceHandle, *, count=2):
    proxies = [
        await space.proxies.create(f"http://u:pw-{n}@127.0.0.1:{8950 + n}", group="g", max_identities=1)
        for n in range(count)
    ]
    await space.proxies.configure_group("g", capacity_accounting="allocations", actor="test", reason="enable")
    identity = (
        await register(
            space, service="fake_tg", external_ref="a", lifecycle="ready", session={"auth_key": "k"}
        )
    ).identity_id
    return proxies, identity


def _pool(space, **policy):
    return space.pools.define(
        "p", service="fake_tg", holder="worker", proxy_policy=ProxyPolicy(group="g", **policy)
    )


async def test_deferred_acquire_is_atomic_and_release_returns_slot(space: SpaceHandle) -> None:
    proxies, identity = await _setup(space, count=1)
    pool = _pool(space, binding=ProxyBindingPolicy())
    first = await pool.acquire(among=[identity], idempotency_key="job")
    assert first.proxy_id == proxies[0].id and first.proxy_assignment_state == "reserved"
    assert first.proxy_allocation_id is not None
    assert "pw-0" in first.proxy.reveal()
    async with space._tx() as tx:
        allocation = await tx.proxy_allocation(first.proxy_allocation_id)
        row = await tx.current_lease(identity)
        assert allocation.owner_lease_id == first.lease_id and allocation.state == "reserved"
        assert row.proxy_allocation_id == allocation.id
        assert row.proxy_request == {
            **allocation.policy,
            "allocation_policy_fingerprint": allocation.policy_fingerprint,
        }
        assert "pw-0" not in str(row.proxy_request)
        assert await tx.proxy_bindings(identity_id=identity) == []
    again = await pool.acquire(among=[identity], idempotency_key="job")
    assert (again.lease_id, again.proxy_id, again.proxy_allocation_id) == (
        first.lease_id,
        first.proxy_id,
        first.proxy_allocation_id,
    )
    await again.release()
    await first.release()
    async with space._tx() as tx:
        row = await tx.get_proxy(proxies[0].id)
        assert (row.bound_count, row.reserved_count, row.draining_count) == (0, 0, 0)
        assert (await tx.proxy_allocation(first.proxy_allocation_id)).state == "released"


async def test_transient_strict_use_never_creates_binding(space: SpaceHandle) -> None:
    proxies, identity = await _setup(space, count=1)
    async with _pool(space, sticky=False).lease(among=[identity]) as lease:
        assert lease.proxy_assignment_state == "reserved"
        async with space._tx() as tx:
            assert await tx.proxy_bindings(identity_id=identity) == []
            assert (await tx.get_proxy(proxies[0].id)).reserved_count == 1
    async with space._tx() as tx:
        assert (await tx.get_proxy(proxies[0].id)).reserved_count == 0


async def test_immediate_binding_reuses_full_slot_without_second_charge(space: SpaceHandle) -> None:
    proxies, identity = await _setup(space, count=1)
    pool = _pool(space)
    async with pool.lease(among=[identity]) as first:
        generation = first.proxy_allocation_id
        assert first.proxy_assignment_state == "bound"
    async with pool.lease(among=[identity]) as second:
        assert second.proxy_allocation_id == generation and second.proxy_id == proxies[0].id
        async with space._tx() as tx:
            row = await tx.get_proxy(proxies[0].id)
            assert (row.bound_count, row.reserved_count, row.draining_count) == (1, 0, 0)


async def test_replay_after_admin_replace_keeps_issued_proxy(space: SpaceHandle) -> None:
    proxies, identity = await _setup(space)
    pool = _pool(space)
    first = await pool.acquire(among=[identity], idempotency_key="job")
    replacement = next(proxy for proxy in proxies if proxy.id != first.proxy_id)
    await space.proxies.rebind(identity, replacement.id, actor="admin", reason="replace")
    replay = await pool.acquire(idempotency_key="job")
    assert (replay.lease_id, replay.proxy_id, replay.proxy_allocation_id) == (
        first.lease_id,
        first.proxy_id,
        first.proxy_allocation_id,
    )
    assert replay.proxy_assignment_state == "draining" and replay.proxy.reveal() == first.proxy.reveal()
    await replay.release()
    await first.release()
    async with space._tx() as tx:
        assert (await tx.get_proxy(first.proxy_id)).draining_count == 0
        assert (await tx.get_proxy(replacement.id)).bound_count == 1


async def test_strict_policy_change_conflicts_with_same_key(space: SpaceHandle) -> None:
    _, identity = await _setup(space, count=1)
    first = await _pool(space, binding=ProxyBindingPolicy()).acquire(among=[identity], idempotency_key="job")
    changed = _pool(space, binding=ProxyBindingPolicy(), max_identities=1)
    with pytest.raises(IdempotencyConflict):
        await changed.acquire(idempotency_key="job")
    await first.release()


async def test_actual_managed_binding_is_tracked_through_legacy_group_policy(space: SpaceHandle) -> None:
    proxies, identity = await _setup(space, count=1)
    await space.proxies.create("http://127.0.0.1:8960", group="legacy")
    await space.proxies.rebind(identity, proxies[0].id, actor="admin", reason="trusted")
    pool = space.pools.define(
        "p", service="fake_tg", holder="worker", proxy_policy=ProxyPolicy(group="legacy")
    )
    first = await pool.acquire(among=[identity], idempotency_key="job")
    assert first.proxy_allocation_id is not None and first.proxy_id == proxies[0].id
    again = await pool.acquire(idempotency_key="job")
    assert again.proxy_allocation_id == first.proxy_allocation_id
    pool.proxy_policy = ProxyPolicy(group="legacy", max_identities=1)
    with pytest.raises(IdempotencyConflict):
        await pool.acquire(idempotency_key="job")
    await first.release()


async def test_failure_after_lease_insert_rolls_back_reservation_and_key(
    space: SpaceHandle, monkeypatch
) -> None:
    proxies, identity = await _setup(space, count=1)
    async with space._tx() as tx:
        backend = type(tx)
    original = backend.acquire_lease

    async def fail(self, *args, **kwargs):
        await original(self, *args, **kwargs)
        raise RuntimeError("rollback after lease insertion")

    pool = _pool(space, binding=ProxyBindingPolicy())
    with monkeypatch.context() as patch:
        patch.setattr(backend, "acquire_lease", fail)
        with pytest.raises(RuntimeError, match="rollback after"):
            await pool.acquire(among=[identity], idempotency_key="job")
    async with space._tx() as tx:
        assert await tx.proxy_allocations(identity_id=identity) == []
        assert (await tx.get_proxy(proxies[0].id)).reserved_count == 0
        assert await tx.find_live_lease("job") is None
    async with pool.lease(among=[identity], idempotency_key="job"):
        pass


async def test_wrong_release_token_cannot_free_current_reservation(space: SpaceHandle) -> None:
    proxies, identity = await _setup(space, count=1)
    lease = await _pool(space, binding=ProxyBindingPolicy()).acquire(among=[identity])
    async with space._tx() as tx:
        outcome = await tx.release_lease(
            replace(lease._row, token=uuid4()), "released", LeaseCounters.empty(), shared=False
        )
        assert outcome == "closed"
        assert (await tx.get_proxy(proxies[0].id)).reserved_count == 1
        assert (await tx.proxy_allocation(lease.proxy_allocation_id)).state == "reserved"
    await lease.release()


async def test_ended_owner_can_be_replaced_without_double_free(space: SpaceHandle) -> None:
    proxies, identity = await _setup(space, count=1)
    pool = _pool(space, binding=ProxyBindingPolicy())
    old = await pool.acquire(among=[identity])
    async with space._tx() as tx:
        await tx.revoke_lease(identity)
    fresh = await pool.acquire(among=[identity])
    assert fresh.proxy_id == old.proxy_id and fresh.proxy_allocation_id != old.proxy_allocation_id
    with pytest.raises(LeaseRevoked):
        await old.release()
    async with space._tx() as tx:
        assert (await tx.get_proxy(proxies[0].id)).reserved_count == 1
        assert (await tx.proxy_allocation(fresh.proxy_allocation_id)).state == "reserved"
    await fresh.release()


async def test_public_competitors_for_last_slot_have_one_winner(space: SpaceHandle) -> None:
    _, first = await _setup(space, count=1)
    second = (
        await register(
            space, service="fake_tg", external_ref="b", lifecycle="ready", session={"auth_key": "k"}
        )
    ).identity_id
    pool = _pool(space, binding=ProxyBindingPolicy())
    results = await asyncio.gather(
        *(pool.acquire(among=[identity]) for identity in (first, second)), return_exceptions=True
    )
    assert sum(isinstance(result, LeaseUnavailable) for result in results) == 1
    for result in results:
        if not isinstance(result, BaseException):
            await result.release()


async def test_shared_strict_issuance_is_rejected_before_lease_or_reservation(space: SpaceHandle) -> None:
    proxies, _ = await _setup(space, count=1)
    service = replace(FAKE_TG, name="shared_proxy", lease_mode=LeaseMode.SHARED)
    space.hub.register_service(service)
    await space.hub.start()
    identity = (
        await register(
            space, service=service.name, external_ref="shared", lifecycle="ready", session={"auth_key": "k"}
        )
    ).identity_id
    pool = space.pools.define("shared", service=service.name, proxy_policy=ProxyPolicy(group="g"))
    with pytest.raises(ValidationFailed, match="exclusive"):
        await pool.acquire(among=[identity])
    async with space._tx() as tx:
        assert await tx.current_lease(identity) is None
        assert await tx.proxy_allocations(identity_id=identity) == []
        assert (await tx.get_proxy(proxies[0].id)).reserved_count == 0


async def test_foreign_space_cannot_release_owner_or_allocation(space: SpaceHandle) -> None:
    proxies, identity = await _setup(space, count=1)
    lease = await _pool(space, binding=ProxyBindingPolicy()).acquire(among=[identity])
    foreign = space.hub.space("foreign-release", create=True)
    async with foreign._tx() as tx:
        assert await tx.release_lease(lease._row, "released", LeaseCounters.empty(), shared=False) == "closed"
    async with space._tx() as tx:
        await tx.check_fence(lease.fence)
        assert (await tx.get_proxy(proxies[0].id)).reserved_count == 1
    await lease.release()


async def test_transient_use_does_not_duplicate_preexisting_full_binding(space: SpaceHandle) -> None:
    proxies, identity = await _setup(space, count=1)
    await space.proxies.rebind(identity, proxies[0].id, actor="admin", reason="trusted")
    async with space._tx() as tx:
        generation = (await tx.proxy_bindings(identity_id=identity))[0].allocation_id
    async with _pool(space, sticky=False).lease(among=[identity]) as lease:
        assert lease.proxy_allocation_id == generation
        async with space._tx() as tx:
            row = await tx.get_proxy(proxies[0].id)
            assert (row.bound_count, row.reserved_count, row.draining_count) == (1, 0, 0)
    async with space._tx() as tx:
        assert (await tx.proxy_bindings(identity_id=identity))[0].allocation_id == generation


async def test_failure_after_promotion_and_outbox_write_rolls_back_everything(
    space: SpaceHandle, monkeypatch
) -> None:
    proxies, identity = await _setup(space, count=1)
    pool = _pool(space)
    async with space._tx() as tx:
        backend = type(tx)
    original = backend.emit

    async def reject_after_emit(self, drafts):
        result = await original(self, drafts)
        if any(draft.type == "lease.acquired" for draft in drafts):
            raise RuntimeError("rollback after promotion and outbox")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(backend, "emit", reject_after_emit)
        with pytest.raises(RuntimeError, match="rollback after promotion"):
            await pool.acquire(among=[identity], idempotency_key="job")
    async with space._tx() as tx:
        assert await tx.proxy_allocations(identity_id=identity, include_released=True) == []
        assert await tx.proxy_bindings(identity_id=identity) == []
        assert await tx.find_live_lease("job") is None
        row = await tx.get_proxy(proxies[0].id)
        assert (row.bound_count, row.reserved_count, row.draining_count) == (0, 0, 0)
    async with pool.lease(among=[identity], idempotency_key="job") as lease:
        assert lease.proxy_assignment_state == "bound"
