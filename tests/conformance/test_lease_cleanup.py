"""F2.2: чистильщик истёкших аренд и удаление аккаунта с арендой (R2-D01, R2-D14, R2-P09)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from tests.fixtures.api import register

from identity_hub import IdentityCreate, IdentityHub, NotFound, SpaceHandle, ValidationFailed
from identity_hub.clock import FakeClock
from identity_hub.maintenance import MaintenanceSchedule
from identity_hub.pool import Pool
from identity_hub.read.events import EventFilters

pytestmark = pytest.mark.conformance

BATCH = 20
RELEASED = 30  # больше размера пачки чистильщика: в ревью — 600 строк при пачке 500


def _short_pool(space: SpaceHandle, name: str = "short") -> Pool:
    return space.pools.define(
        name,
        service="fake_tg",
        holder="worker-1",
        ttl=timedelta(seconds=0.3),
        heartbeat=timedelta(seconds=0.1),
        safety_margin=timedelta(seconds=0.1),
    )


async def _let_expire(hub: IdentityHub) -> None:
    """Воркер упал: время идёт, heartbeat нет."""
    if isinstance(hub.clock, FakeClock):
        hub.clock.advance(1)
    else:
        await asyncio.sleep(0.5)


async def _events(space: SpaceHandle, type_: str) -> list[Any]:
    page = await space.admin.event_page(filters=EventFilters(type=type_, include_journal=True))
    return list(page.items)


async def _live_lease(space: SpaceHandle, identity_id: UUID) -> Any:
    async with space._tx(own=True) as tx:
        return await tx.current_lease(identity_id)


async def test_abandoned_lease_is_closed_in_one_tick_behind_many_released(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    """R2-D01: давно освобождённые аренды не заслоняют брошенную от чистильщика."""
    await space.identities.create_many(
        [
            IdentityCreate(
                service="fake_tg", external_ref=f"+{n}", lifecycle="ready", session={"auth_key": "k"}
            )
            for n in range(RELEASED + 1)
        ],
        actor="test",
    )
    hub_under_test.maintenance.schedule = MaintenanceSchedule(batch_size=BATCH)
    pool = space.pools.define("work", service="fake_tg", holder="worker-1")
    for _ in range(RELEASED):
        lease = await pool.acquire()
        await lease.release()
    abandoned = await _short_pool(space).acquire()  # release() не будет: воркер упал
    await _let_expire(hub_under_test)

    report = await hub_under_test.maintenance.run_all()

    assert report.counts["leases"] == 1
    (history,) = await space.admin.lease_history(abandoned.identity_id)
    assert history.end_reason == "expired" and history.released_at is not None
    (expired,) = await _events(space, "lease.expired")
    assert expired.lease_id == abandoned.lease_id
    again = await hub_under_test.maintenance.run_all()
    assert again.counts["leases"] == 0


async def test_lease_expired_event_has_the_same_fields_on_every_backend(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    """R2-P09: `lease.expired` от обслуживания несёт `pool`, `holder`, `fencing`, `expired_at`, `reason`."""
    await register(space, service="fake_tg", external_ref="+1", lifecycle="ready", session={"auth_key": "k"})
    lease = await _short_pool(space).acquire()
    await _let_expire(hub_under_test)

    await hub_under_test.maintenance.run_all()

    (event,) = await _events(space, "lease.expired")
    assert (event.actor, event.identity_id, event.lease_id) == (
        "maintenance",
        lease.identity_id,
        lease.lease_id,
    )
    assert set(event.data) == {"pool", "holder", "fencing", "expired_at", "reason"}
    assert event.data["reason"] == "expired"  # N1.2: на потолке удержания было бы max_hold
    assert (event.data["pool"], event.data["holder"], event.data["fencing"]) == (
        "short",
        "worker-1",
        lease.fencing,
    )
    (history,) = await space.admin.lease_history(lease.identity_id)
    assert history.released_at is not None
    assert event.data["expired_at"] == history.released_at.isoformat()


async def test_identity_with_live_lease_is_not_deleted_silently(space: SpaceHandle) -> None:
    """R2-D14: удаление не уносит строку аренды каскадом, оставляя историю открытой навсегда."""
    reg = await register(
        space, service="fake_tg", external_ref="+1", lifecycle="ready", session={"auth_key": "k"}
    )
    lease = await space.pools.define("work", service="fake_tg", holder="worker-1").acquire()

    with pytest.raises(ValidationFailed):
        await space.identities.delete(reg.identity_id, actor="admin")

    assert (await space.identities.get(reg.identity_id)).id == reg.identity_id
    assert await _live_lease(space, reg.identity_id) is not None
    await lease.release()
    await space.identities.delete(reg.identity_id, actor="admin")  # без аренды удаление штатное
    with pytest.raises(NotFound):
        await space.identities.get(reg.identity_id)


async def test_delete_with_revoke_closes_lease_history_in_the_same_transaction(space: SpaceHandle) -> None:
    reg = await register(
        space, service="fake_tg", external_ref="+1", lifecycle="ready", session={"auth_key": "k"}
    )
    lease = await space.pools.define("work", service="fake_tg", holder="worker-1").acquire()

    await space.identities.delete(reg.identity_id, actor="admin", revoke=True)

    with pytest.raises(NotFound):
        await space.identities.get(reg.identity_id)
    (history,) = await space.admin.lease_history(reg.identity_id)  # история переживает удаление
    assert history.lease_id == lease.lease_id
    assert history.end_reason == "revoked" and history.released_at is not None
    (revoked,) = await _events(space, "lease.revoked")
    assert (revoked.actor, revoked.lease_id) == ("admin", lease.lease_id)
    assert revoked.data["pool"] == "work" and revoked.data["holder"] == "worker-1"
    assert len(await _events(space, "identity.deleted")) == 1
