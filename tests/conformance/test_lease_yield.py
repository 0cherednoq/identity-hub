"""N3.2b: доставка уступки, yielded и принудительный отзыв после grace."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from identity_hub import IdentityCreate, IdentityHub, LeaseUnavailable, SpaceHandle
from identity_hub.clock import FakeClock

pytestmark = [pytest.mark.anyio, pytest.mark.conformance]

SINCE = datetime(2000, 1, 1, tzinfo=UTC)


async def _ready(space: SpaceHandle) -> None:
    await space.identities.create(
        IdentityCreate(
            service="fake_tg",
            external_ref="+yield-holder",
            lifecycle="ready",
            session={"auth_key": "k"},
        ),
        actor="test",
    )


async def _history(space: SpaceHandle, lease_id: Any) -> Any:
    async with space._tx(own=True) as tx:
        rows = await tx.lease_read_rows(SINCE)
    return next(row for row in rows if row.lease_id == lease_id)


async def test_local_callback_and_normal_release_are_yielded(space: SpaceHandle) -> None:
    await _ready(space)
    called = asyncio.Event()
    background = space.pools.define("background", service="fake_tg", priority=0)
    urgent = space.pools.define("urgent", service="fake_tg", priority=10)
    lease = await background.acquire()
    lease._start(None, called.set)

    with pytest.raises(LeaseUnavailable):
        await urgent.acquire()
    await asyncio.wait_for(called.wait(), 1)
    assert lease.yield_requested

    await lease.release()
    assert (await _history(space, lease.lease_id)).end_reason == "yielded"


async def test_grace_forces_yield_and_reserves_for_requester(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    await _ready(space)
    grace = timedelta(seconds=2)
    background = space.pools.define("background", service="fake_tg", priority=0)
    opportunistic = space.pools.define("opportunistic", service="fake_tg", priority=1)
    urgent = space.pools.define("urgent", service="fake_tg", priority=10, yield_grace=grace)
    victim = await background.acquire()

    with pytest.raises(LeaseUnavailable):
        await urgent.acquire()
    if isinstance(hub_under_test.clock, FakeClock):
        hub_under_test.clock.advance(grace + timedelta(milliseconds=1))
    else:
        await asyncio.sleep(grace.total_seconds() + 0.05)
    with pytest.raises(LeaseUnavailable):  # этот опрос отзывает; следующий уже выбирает
        await urgent.acquire()
    with pytest.raises(LeaseUnavailable):
        await opportunistic.acquire()

    replacement = await urgent.acquire()
    assert replacement.identity_id == victim.identity_id
    assert (await _history(space, victim.lease_id)).end_reason == "yielded"
    await replacement.release()
