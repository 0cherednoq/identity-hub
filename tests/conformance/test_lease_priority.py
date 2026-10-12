"""N3.2a: приоритет аренды и кооперативный запрос уступить."""

from __future__ import annotations

from dataclasses import replace

import pytest
from tests.fixtures.services import FAKE_TG

from identity_hub import (
    IdentityCreate,
    LeaseMode,
    LeaseUnavailable,
    Pool,
    SpaceHandle,
    ValidationFailed,
)
from identity_hub.read.events import EventFilters

pytestmark = [pytest.mark.conformance]


async def _ready(space: SpaceHandle, count: int) -> None:
    await space.identities.create_many(
        [
            IdentityCreate(
                service="fake_tg",
                external_ref=f"+priority-{count}-{index}",
                lifecycle="ready",
                session={"auth_key": "k"},
            )
            for index in range(count)
        ],
        actor="test",
    )


async def test_higher_priority_requests_exactly_one_victim(space: SpaceHandle) -> None:
    await _ready(space, 2)
    background = space.pools.define("background", service="fake_tg", priority=0)
    urgent = space.pools.define("urgent", service="fake_tg", priority=10)
    leases = [await background.acquire(), await background.acquire()]

    with pytest.raises(LeaseUnavailable):
        await urgent.acquire()
    await leases[0].renew()
    await leases[1].renew()
    requested = [lease for lease in leases if lease.yield_requested]
    assert len(requested) == 1

    with pytest.raises(LeaseUnavailable):
        await urgent.acquire()
    await leases[0].renew()
    await leases[1].renew()
    assert sum(lease.yield_requested for lease in leases) == 1

    events = await space.admin.event_page(
        filters=EventFilters(type="lease.yield_requested", include_journal=True)
    )
    assert len(events.items) == 1
    victim = requested[0]
    await victim.release()
    replacement = await urgent.acquire()
    assert replacement.priority == 10 and not replacement.yield_requested
    await replacement.release()
    for lease in leases:
        if lease is not victim:
            await lease.release()


async def test_equal_priority_does_not_request_yield(space: SpaceHandle) -> None:
    await _ready(space, 1)
    first = space.pools.define("first", service="fake_tg", priority=5)
    peer = space.pools.define("peer", service="fake_tg", priority=5)
    lease = await first.acquire()
    with pytest.raises(LeaseUnavailable):
        await peer.acquire()
    await lease.renew()
    assert not lease.yield_requested
    await lease.release()


async def test_priority_validation_and_shared_rejection(space: SpaceHandle) -> None:
    with pytest.raises(ValidationFailed):
        space.pools.define("bad", service="fake_tg", priority=True)  # type: ignore[arg-type]
    shared = Pool(
        space=space,
        name="shared",
        service=replace(FAKE_TG, lease_mode=LeaseMode.SHARED),
    )
    with pytest.raises(ValidationFailed, match="SHARED"):
        shared.lease_options(purpose=None, ref=None, priority=0)
