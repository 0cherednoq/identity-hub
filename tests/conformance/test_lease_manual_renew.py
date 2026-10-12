"""N2.1 (задача 7): ручное продление аренды из ``pool.acquire()``."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from identity_hub import IdentityCreate, IdentityHub, LeaseLost, LeaseRevoked, SpaceHandle
from identity_hub.clock import FakeClock

pytestmark = [pytest.mark.conformance]

SINCE = datetime(2000, 1, 1, tzinfo=UTC)
TIMING = {
    "ttl": timedelta(seconds=0.6),
    "heartbeat": timedelta(seconds=0.2),
    "safety_margin": timedelta(seconds=0.3),
    "stats_flush": timedelta(seconds=0.2),
}


async def _ready(space: SpaceHandle) -> None:
    await space.identities.create(
        IdentityCreate(service="fake_tg", external_ref="+1", lifecycle="ready", session={"auth_key": "k"}),
        actor="test",
    )


async def _pass(hub: IdentityHub, seconds: float) -> None:
    if isinstance(hub.clock, FakeClock):
        hub.clock.advance(timedelta(seconds=seconds))
    else:
        await asyncio.sleep(seconds)


async def _history(space: SpaceHandle, lease_id: Any) -> Any:
    async with space._tx(own=True) as tx:
        rows = await tx.lease_read_rows(SINCE)
    return next(row for row in rows if row.lease_id == lease_id)


async def test_manual_renew_keeps_acquired_lease_past_ttl(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    await _ready(space)
    pool = space.pools.define("work", service="fake_tg", holder="w", **TIMING)
    lease = await pool.acquire()
    original_expiry = lease.expires_at

    for _ in range(4):
        await _pass(hub_under_test, 0.2)
        renewed_until = await lease.renew()
        assert renewed_until == lease.expires_at

    assert lease.expires_at > original_expiry
    lease.check()
    await lease.release()


async def test_manual_renew_reports_revoke(space: SpaceHandle) -> None:
    await _ready(space)
    pool = space.pools.define("work", service="fake_tg", holder="w", **TIMING)
    lease = await pool.acquire()
    assert await space.pools.revoke(lease.identity_id, reason="тест", actor="admin")

    with pytest.raises(LeaseRevoked) as caught:
        await lease.renew()
    assert caught.value.reason == "revoked"


async def test_manual_renew_flushes_counters(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    await _ready(space)
    lease = await space.pools.define(
        "work",
        service="fake_tg",
        holder="w",
        ttl=timedelta(seconds=2),
        heartbeat=timedelta(seconds=0.2),
        safety_margin=timedelta(seconds=0.5),
        stats_flush=timedelta(seconds=0.2),
    ).acquire()
    async with lease.operation("read_channel"):
        pass
    await _pass(hub_under_test, 0.25)

    await lease.renew()

    history = await _history(space, lease.lease_id)
    assert (history.operations, history.succeeded) == ({"read_channel": 1}, 1)
    await lease.release()


async def test_manual_renew_respects_hold_deadline(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    await _ready(space)
    lease = await space.pools.define("work", service="fake_tg", holder="w", **TIMING).acquire(
        max_hold=timedelta(seconds=1)
    )
    await _pass(hub_under_test, 0.2)
    await lease.renew()
    await _pass(hub_under_test, 0.2)

    assert await lease.renew() == lease.hold_deadline
    await _pass(hub_under_test, 0.4)
    with pytest.raises(LeaseLost) as caught:
        await lease.renew()
    assert caught.value.reason == "max_hold"


async def test_manual_renew_is_allowed_inside_lease_scope(space: SpaceHandle) -> None:
    await _ready(space)
    pool = space.pools.define("work", service="fake_tg", holder="w", **TIMING)

    async with pool.lease() as lease:
        assert await lease.renew() == lease.expires_at
