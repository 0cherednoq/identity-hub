"""IMP-06 / R1-16 (I2.4): renew, release и revoke — таблица сценариев (D-213)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from identity_hub import IdentityCreate, IdentityHub, LeaseRevoked, SpaceHandle
from identity_hub.clock import FakeClock
from identity_hub.errors import LeaseLost
from identity_hub.read.events import EventFilters

pytestmark = [pytest.mark.conformance]

SINCE = datetime(2000, 1, 1, tzinfo=UTC)
SHORT = {
    "ttl": timedelta(seconds=0.6),
    "heartbeat": timedelta(seconds=0.2),
    "safety_margin": timedelta(seconds=0.3),
    "stats_flush": timedelta(seconds=0.2),
}


async def _ready(space: SpaceHandle, count: int = 1) -> None:
    await space.identities.create_many(
        [
            IdentityCreate(
                service="fake_tg", external_ref=f"+{n}", lifecycle="ready", session={"auth_key": "k"}
            )
            for n in range(count)
        ],
        actor="test",
    )


async def _history(space: SpaceHandle, lease_id: Any) -> Any:
    async with space._tx(own=True) as tx:
        rows = await tx.lease_read_rows(SINCE)
    (row,) = [row for row in rows if row.lease_id == lease_id]
    return row


async def _types(space: SpaceHandle, lease_id: Any) -> list[str]:
    page = await space.admin.event_page(filters=EventFilters(lease_id=lease_id, include_journal=True))
    return sorted(item.type for item in page.items)


async def _expire(hub: IdentityHub, lease: Any) -> None:
    if isinstance(hub.clock, FakeClock):
        hub.clock.advance(timedelta(seconds=2))
    else:
        await asyncio.sleep(0.8)


async def test_renew_never_shortens(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    await _ready(space)
    lease = await space.pools.define("work", service="fake_tg", holder="w").acquire()
    before = lease.expires_at

    renewed = await hub_under_test._store.renew_leases([lease._row.token], 1.0)

    assert renewed[lease._row.token].expires_at >= before
    await lease.release()


async def test_release_own_then_repeat_is_noop(space: SpaceHandle) -> None:
    await _ready(space)
    lease = await space.pools.define("work", service="fake_tg", holder="w").acquire()

    await lease.release()
    await lease.release()

    assert (await _history(space, lease.lease_id)).end_reason == "released"
    assert await _types(space, lease.lease_id) == ["lease.acquired", "lease.released"]


async def test_release_after_natural_expiry_is_success(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    await _ready(space)
    lease = await space.pools.define("work", service="fake_tg", holder="w", **SHORT).acquire()
    await _expire(hub_under_test, lease)

    await lease.release()

    history = await _history(space, lease.lease_id)
    assert history.end_reason == "expired"
    assert "lease.expired" in await _types(space, lease.lease_id)


async def test_release_after_takeover_is_success(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    await _ready(space)
    pool = space.pools.define("work", service="fake_tg", holder="w", **SHORT)
    old = await pool.acquire()
    await _expire(hub_under_test, old)
    new = await pool.acquire()
    assert new.identity_id == old.identity_id

    await old.release()

    assert (await _history(space, old.lease_id)).end_reason == "expired"
    assert (await _history(space, new.lease_id)).released_at is None  # чужую аренду не тронули
    await new.release()


async def test_release_of_revoked_lease_raises_once(space: SpaceHandle) -> None:
    await _ready(space)
    lease = await space.pools.define("work", service="fake_tg", holder="w").acquire()
    assert await space.pools.revoke(lease.identity_id, reason="тест", actor="admin")
    assert not await space.pools.revoke(lease.identity_id, reason="тест", actor="admin")

    with pytest.raises(LeaseRevoked):
        await lease.release()
    await lease.release()  # повтор — no-op

    assert (await _history(space, lease.lease_id)).end_reason == "revoked"


async def test_revoke_wakes_live_lease_in_same_process(space: SpaceHandle) -> None:
    await _ready(space)
    lost, started = asyncio.Event(), asyncio.Event()
    pool = space.pools.define(
        "work",
        service="fake_tg",
        holder="w",
        ttl=timedelta(minutes=10),
        heartbeat=timedelta(minutes=3),
        safety_margin=timedelta(minutes=3),
    )
    held: list[Any] = []

    async def worker() -> None:
        async with pool.lease(on_lost=lost.set) as lease:
            held.append(lease)
            started.set()
            await asyncio.sleep(10)

    task = asyncio.create_task(worker())
    await started.wait()
    assert await space.pools.revoke(held[0].identity_id, reason="тест", actor="admin")

    await asyncio.wait_for(lost.wait(), timeout=5)  # не ждём heartbeat через 3 минуты
    with pytest.raises(LeaseRevoked):
        await task


async def test_lost_after_completed_body_is_marked(space: SpaceHandle) -> None:
    """R1-16: тело блока завершилось, аренду потеряли по пути — `LeaseLost(body_completed=True)`."""
    await _ready(space)
    pool = space.pools.define("work", service="fake_tg", holder="w")

    with pytest.raises(LeaseLost) as caught:
        async with pool.lease() as lease:
            lease._lost = "heartbeat"  # потеря замечена, но тело дошло до конца

    assert caught.value.body_completed is True
