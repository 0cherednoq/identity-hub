"""PA5/R12: техническая ошибка не подтверждает резерв по прежнему auth."""

from datetime import timedelta

import pytest
from tests.conformance.test_proxy_allocation_finalization import _binding, _setup

from identity_hub import AllOf, AnyOf, Blame, ProxyBindingPolicy, RegionIn
from identity_hub.protocols import Fault
from identity_hub.read.events import EventFilters

pytestmark = pytest.mark.conformance


@pytest.mark.parametrize(
    "criterion,confirmed",
    [
        (RegionIn("auth", states={"authenticated"}), False),
        (
            AnyOf(RegionIn("auth", states={"authenticated"}), RegionIn("availability", states={"available"})),
            False,
        ),
        (
            AllOf(RegionIn("auth", states={"authenticated"}), RegionIn("availability", states={"cooling"})),
            True,
        ),
        (
            AnyOf(RegionIn("auth", states={"authenticated"}), RegionIn("availability", states={"cooling"})),
            True,
        ),
    ],
)
async def test_fault_only_proves_a_new_matching_branch(hub_under_test, space, criterion, confirmed):
    proxy, identity, adapter, service = await _setup(
        hub_under_test, space, binding=ProxyBindingPolicy(bind_when=criterion)
    )
    adapter.fault = Fault(Blame.IDENTITY, retry_after=timedelta(seconds=1))
    lease = await space.pools.define("worker", service=service).acquire(among=[identity])
    with pytest.raises(RuntimeError, match="temporary"):
        async with lease.operation("work"):
            raise RuntimeError("temporary")
    assert bool(await _binding(space, identity)) is confirmed
    assert (await space.identities.get(identity)).availability == "cooling"
    events = await space.admin.event_page(filters=EventFilters(type="proxy.reservation.confirmed"))
    assert len(events.items) == int(confirmed)
    if not confirmed:
        await lease.mark_authenticated()  # Явное принятое подтверждение auth под владельцем.
        assert await _binding(space, identity)
    await lease.release()
    assert (await space.admin.proxy_detail(proxy.id)).proxy.reserved_count == 0


async def test_unknown_probe_result_does_not_use_previous_good_auth(hub_under_test, space):
    proxy, identity, adapter, _ = await _setup(hub_under_test, space)
    adapter.error = RuntimeError("unknown response")
    result = await space.auth.verify_session(identity, actor="checker")
    assert str(result.outcome) == "failed"
    assert await _binding(space, identity) == []
    assert (await space.admin.proxy_detail(proxy.id)).proxy.occupied_count == 0
    assert not (await space.admin.event_page(filters=EventFilters(type="proxy.reservation.confirmed"))).items
