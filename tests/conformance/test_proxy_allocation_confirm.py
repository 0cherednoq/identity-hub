"""PA2.2: ручное подтверждение проверяет владельца, правила и поколения."""

from dataclasses import replace
from uuid import uuid4

import pytest
from tests.fixtures.api import register

from identity_hub import (
    AllOf,
    Lease,
    LeaseLost,
    LeaseOwnershipLost,
    Override,
    ProxyAssignment,
    ProxyAssignmentConflict,
    ProxyBindingPolicy,
    ProxyPolicy,
    RegionIn,
    SpaceHandle,
    ValidationFailed,
)
from identity_hub.read.events import EventFilters

pytestmark = pytest.mark.conformance


async def _setup(space, binding=None, count=1):
    proxies = [
        await space.proxies.create(f"http://u:pw@127.0.0.1:{8980 + n}", group="g", max_identities=1)
        for n in range(count)
    ]
    await space.proxies.configure_group("g", capacity_accounting="allocations", actor="test", reason="enable")
    identity = (
        await register(
            space, service="fake_tg", external_ref="a", lifecycle="ready", session={"auth_key": "k"}
        )
    ).identity_id
    pool = space.pools.define(
        "p",
        service="fake_tg",
        holder="worker",
        proxy_policy=ProxyPolicy(group="g", binding=binding or ProxyBindingPolicy()),
    )
    return proxies, identity, pool


async def test_confirmation_is_idempotent_and_keeps_one_slot(space: SpaceHandle) -> None:
    proxies, identity, pool = await _setup(space)
    lease = await pool.acquire(among=[identity])
    result = await lease.confirm_proxy(reason="application accepted")
    assert isinstance(result, ProxyAssignment) and result.state == "bound"
    assert result.allocation_id == lease.proxy_allocation_id and result.proxy_id == lease.proxy_id
    assert lease.proxy_assignment_state == "bound"
    assert await lease.confirm_proxy(reason="repeat") == result
    assert "pw" not in repr(result) and "token" not in repr(result)
    events = (await space.admin.event_page(filters=EventFilters(type="proxy.binding.changed"))).items
    assert len(events) == 1 and events[0].actor == "worker"
    assert events[0].data["allocation_id"] == str(result.allocation_id)
    async with space._tx() as tx:
        row = await tx.get_proxy(proxies[0].id)
        assert (row.bound_count, row.reserved_count, row.draining_count) == (1, 0, 0)
        assert (await tx.proxy_allocation(result.allocation_id)).owner_lease_id is None
    await lease.release()
    with pytest.raises(LeaseLost):
        await lease.confirm_proxy(reason="after release")


async def test_bind_rule_uses_accepted_state_and_does_not_mutate_on_failure(space: SpaceHandle) -> None:
    _, identity, pool = await _setup(
        space, ProxyBindingPolicy(bind_when=RegionIn("standing", states={"limited"}))
    )
    lease = await pool.acquire(among=[identity])
    with pytest.raises(ProxyAssignmentConflict, match="condition_not_met"):
        await lease.confirm_proxy(reason="too early")
    assert lease.proxy_assignment_state == "reserved"
    await space.identities.override(
        identity, Override("standing", None, "limited", reason="accepted", actor="test")
    )
    assert (await lease.confirm_proxy(reason="accepted application result")).state == "bound"
    await lease.release()


async def test_column_and_default_auth_facts_are_available(space: SpaceHandle) -> None:
    rule = AllOf(
        RegionIn("lifecycle", states={"ready"}),
        RegionIn("availability", states={"available"}),
        RegionIn("auth", states={"authenticated"}),
    )
    _, identity, pool = await _setup(space, ProxyBindingPolicy(bind_when=rule))
    async with pool.lease(among=[identity]) as lease:
        assert (await lease.confirm_proxy(reason="accepted")).state == "bound"


async def test_negative_rule_wins_over_positive_precondition(space: SpaceHandle) -> None:
    positive = RegionIn("standing", states={"good"})
    _, identity, pool = await _setup(space, ProxyBindingPolicy(bind_when=positive, unbind_when=positive))
    lease = await pool.acquire(among=[identity])
    with pytest.raises(ProxyAssignmentConflict, match="condition_not_met"):
        await lease.confirm_proxy(reason="conflicting rules")
    async with space._tx() as tx:
        assert await tx.proxy_bindings(identity_id=identity) == []
        assert (await tx.proxy_allocation(lease.proxy_allocation_id)).state == "reserved"
    await lease.release()


async def test_transient_assignment_cannot_be_confirmed(space: SpaceHandle) -> None:
    _, identity, _ = await _setup(space)
    pool = space.pools.define(
        "transient", service="fake_tg", proxy_policy=ProxyPolicy(group="g", sticky=False)
    )
    lease = await pool.acquire(among=[identity])
    with pytest.raises(ProxyAssignmentConflict, match="not_reserved"):
        await lease.confirm_proxy(reason="cannot persist transient use")
    await lease.release()


async def test_disabled_proxy_is_not_confirmed(space: SpaceHandle) -> None:
    proxies, identity, pool = await _setup(space)
    lease = await pool.acquire(among=[identity])
    await space.proxies.disable(proxies[0].id, actor="admin", reason="maintenance")
    with pytest.raises(ProxyAssignmentConflict, match="proxy_unavailable"):
        await lease.confirm_proxy(reason="late result")
    assert lease.proxy_assignment_state == "reserved"
    await space.proxies.enable(proxies[0].id, actor="admin")
    assert (await lease.confirm_proxy(reason="available")).state == "bound"
    await lease.release()


async def test_admin_replacement_prevents_old_confirmation(space: SpaceHandle) -> None:
    proxies, identity, pool = await _setup(space, count=2)
    lease = await pool.acquire(among=[identity])
    other = next(proxy for proxy in proxies if proxy.id != lease.proxy_id)
    await space.proxies.rebind(identity, other.id, actor="admin", reason="replacement")
    with pytest.raises(ProxyAssignmentConflict, match="assignment_changed"):
        await lease.confirm_proxy(reason="late result")
    async with space._tx() as tx:
        assert (await tx.proxy_bindings(identity_id=identity))[0].proxy_id == other.id
        assert (await tx.proxy_allocation(lease.proxy_allocation_id)).state == "draining"
    await lease.release()


async def test_forged_token_and_foreign_space_cannot_confirm(space: SpaceHandle) -> None:
    _, identity, pool = await _setup(space)
    lease = await pool.acquire(among=[identity])
    forged = Lease(
        lease.pool,
        replace(lease._row, token=uuid4()),
        lease.identity,
        lease.proxy_info,
        lease._proxy_allocation,
    )
    with pytest.raises(LeaseOwnershipLost):
        await forged.confirm_proxy(reason="wrong token")
    foreign = space.hub.space("foreign-confirm", create=True)
    wrong_pool = foreign.pools.define("p", service="fake_tg", proxy_policy=pool.proxy_policy)
    scoped = Lease(wrong_pool, lease._row, lease.identity, lease.proxy_info, lease._proxy_allocation)
    with pytest.raises(LeaseOwnershipLost):
        await scoped.confirm_proxy(reason="wrong space")
    async with space._tx() as tx:
        assert (await tx.proxy_allocation(lease.proxy_allocation_id)).state == "reserved"
    await lease.release()


@pytest.mark.parametrize("reason", ["", "   ", None])
async def test_confirmation_requires_reason_before_mutation(space: SpaceHandle, reason) -> None:
    _, identity, pool = await _setup(space)
    lease = await pool.acquire(among=[identity])
    with pytest.raises(ValidationFailed):
        await lease.confirm_proxy(reason=reason)
    assert lease.proxy_assignment_state == "reserved"
    async with space._tx() as tx:
        assert (await tx.proxy_allocation(lease.proxy_allocation_id)).state == "reserved"
    await lease.release()
