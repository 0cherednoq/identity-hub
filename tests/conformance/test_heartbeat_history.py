"""IMP-02 (I2.2a): история аренды пишется раз в `stats_flush` и при освобождении (D-210)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from identity_hub import IdentityCreate, IdentityHub, SpaceHandle
from identity_hub.clock import FakeClock

pytestmark = [pytest.mark.conformance]

SINCE = datetime(2000, 1, 1, tzinfo=UTC)


async def _history(space: SpaceHandle, lease_id: UUID) -> Any:
    async with space._tx(own=True) as tx:
        rows = await tx.lease_read_rows(SINCE)
    (row,) = [row for row in rows if row.lease_id == lease_id]
    return row


async def _pass(hub: IdentityHub, seconds: float) -> None:
    if isinstance(hub.clock, FakeClock):
        hub.clock.advance(timedelta(seconds=seconds))
    else:
        await asyncio.sleep(seconds)


async def _lease(space: SpaceHandle, **timing: timedelta) -> Any:
    await space.identities.create(
        IdentityCreate(service="fake_tg", external_ref="+1", lifecycle="ready", session={"auth_key": "k"}),
        actor="test",
    )
    pool = space.pools.define("work", service="fake_tg", holder="w", **timing)
    return await pool.acquire()


async def test_heartbeat_keeps_counters_until_flush_and_release_writes_them(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    lease = await _lease(space, stats_flush=timedelta(hours=1))
    async with lease.operation("read_channel"):
        pass
    await _pass(hub_under_test, 0.05)
    assert await lease._extend() is not None

    live = await _history(space, lease.lease_id)
    assert live.operations == {} and live.succeeded == 0  # счётчики ещё в памяти воркера
    assert live.last_heartbeat_at > live.acquired_at  # свежий heartbeat — из ih_leases

    await lease.release()
    closed = await _history(space, lease.lease_id)
    assert (closed.operations, closed.succeeded) == ({"read_channel": 1}, 1)


async def test_history_flushes_after_interval(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    lease = await _lease(
        space,
        ttl=timedelta(seconds=3),
        heartbeat=timedelta(seconds=0.2),
        safety_margin=timedelta(seconds=0.5),
        stats_flush=timedelta(seconds=0.2),
    )
    async with lease.operation("read_channel"):
        pass
    await _pass(hub_under_test, 0.3)
    assert await lease._extend() is not None

    flushed = await _history(space, lease.lease_id)
    assert (flushed.operations, flushed.succeeded) == ({"read_channel": 1}, 1)
    await lease.release()


def test_stats_flush_must_not_be_shorter_than_heartbeat() -> None:
    from identity_hub import ValidationFailed
    from identity_hub.pool import LeaseTiming

    with pytest.raises(ValidationFailed):
        LeaseTiming(stats_flush=timedelta(seconds=1))
    assert LeaseTiming().stats_flush == timedelta(minutes=5)
