"""N1.2 (задача 4): потолок удержания аренды — продление не выходит за `hold_deadline`."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from tests.fixtures.services import FAKE_TG

from identity_hub import IdentityCreate, IdentityHub, LeaseLost, LeaseMode, SpaceHandle, ValidationFailed
from identity_hub.clock import FakeClock
from identity_hub.enums import LeaseEndReason
from identity_hub.read.events import EventFilters
from identity_hub.store.base import LeaseCounters, LeaseRenewal

pytestmark = [pytest.mark.conformance]

SINCE = datetime(2000, 1, 1, tzinfo=UTC)
TIMING = {
    "ttl": timedelta(seconds=6),
    "heartbeat": timedelta(seconds=0.2),
    "safety_margin": timedelta(seconds=0.3),
}
HOLD = timedelta(seconds=1)


async def _ready(space: SpaceHandle, count: int = 1, service: str = "fake_tg") -> None:
    await space.identities.create_many(
        [
            IdentityCreate(
                service=service, external_ref=f"+{n}", lifecycle="ready", session={"auth_key": "k"}
            )
            for n in range(count)
        ],
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
    (row,) = [row for row in rows if row.lease_id == lease_id]
    return row


async def test_deadline_caps_the_lease_from_the_start(space: SpaceHandle) -> None:
    await _ready(space)
    pool = space.pools.define("work", service="fake_tg", holder="w", **TIMING)

    lease = await pool.acquire(max_hold=HOLD)

    assert lease.hold_deadline == lease.acquired_at + HOLD
    assert lease.expires_at == lease.hold_deadline  # ttl 6 с обрезан дедлайном
    history = await _history(space, lease.lease_id)
    assert history.hold_deadline == lease.hold_deadline
    await lease.release()


async def test_no_deadline_by_default(space: SpaceHandle) -> None:
    await _ready(space)
    lease = await space.pools.define("work", service="fake_tg", holder="w", **TIMING).acquire()

    assert lease.hold_deadline is None and lease.expires_at == lease.acquired_at + TIMING["ttl"]
    await lease.release()


async def test_renewal_never_passes_the_deadline(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    await _ready(space)
    lease = await space.pools.define("work", service="fake_tg", holder="w", **TIMING).acquire(max_hold=HOLD)
    await _pass(hub_under_test, 0.3)

    batch = (await hub_under_test._store.renew_leases([lease._row.token], 60.0))[lease._row.token]
    async with space._tx(own=True) as tx:
        single = await tx.extend_lease(lease._row, 60.0, LeaseCounters.empty(), shared=False)

    assert batch.expires_at == single.expires_at == lease.hold_deadline
    await lease.release()


async def test_batch_caps_each_lease_by_its_own_deadline(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    await _ready(space, 3)
    pool = space.pools.define("work", service="fake_tg", holder="w", **TIMING)
    short = await pool.acquire(max_hold=HOLD)
    long = await pool.acquire(max_hold=timedelta(seconds=30))
    free = await pool.acquire()
    await _pass(hub_under_test, 0.3)

    renewed = await hub_under_test._store.renew_leases([x._row.token for x in (short, long, free)], 60.0)

    assert renewed[short._row.token].expires_at == short.hold_deadline
    assert renewed[long._row.token].expires_at == long.hold_deadline
    assert renewed[free._row.token].expires_at > free.acquired_at + timedelta(seconds=59)
    for lease in (short, long, free):
        await lease.release()


async def test_pool_default_and_call_override(space: SpaceHandle) -> None:
    await _ready(space, 2)
    pool = space.pools.define("work", service="fake_tg", holder="w", max_hold=HOLD, **TIMING)

    default = await pool.acquire()
    custom = await pool.acquire(max_hold=timedelta(seconds=3))

    assert default.hold_deadline == default.acquired_at + HOLD
    assert custom.hold_deadline == custom.acquired_at + timedelta(seconds=3)
    await default.release()
    await custom.release()


@pytest.mark.parametrize("bad", [timedelta(0), timedelta(seconds=-1), timedelta(seconds=0.3)])
async def test_max_hold_must_exceed_safety_margin(space: SpaceHandle, bad: timedelta) -> None:
    await _ready(space)
    pool = space.pools.define("work", service="fake_tg", holder="w", **TIMING)

    with pytest.raises(ValidationFailed):
        space.pools.define("other", service="fake_tg", max_hold=bad, **TIMING)
    with pytest.raises(ValidationFailed):
        await pool.acquire(max_hold=bad)

    lease = await pool.acquire()  # аккаунт не занят неудачной попыткой
    await lease.release()


async def test_holder_loses_the_lease_at_the_deadline(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    await _ready(space)
    pool = space.pools.define("work", service="fake_tg", holder="w", **TIMING)
    lease_id = None

    with pytest.raises(LeaseLost) as caught:
        async with pool.lease(max_hold=HOLD) as lease:
            lease_id = lease.lease_id
            for _ in range(400):  # heartbeat идёт, но дедлайн не продлевается
                if isinstance(hub_under_test.clock, FakeClock):
                    hub_under_test.clock.advance(timedelta(seconds=0.05))
                await asyncio.sleep(0.01)

    assert type(caught.value) is LeaseLost and caught.value.reason == "max_hold"
    history = await _history(space, lease_id)
    assert history.end_reason == "max_hold"
    # потеря раньше дедлайна на safety_margin: аккаунт ещё никому не выдан
    assert history.released_at <= history.hold_deadline
    page = await space.admin.event_page(filters=EventFilters(lease_id=lease_id, include_journal=True))
    (expired,) = [item for item in page.items if item.type == "lease.expired"]
    assert expired.data["reason"] == "max_hold"


async def test_check_reports_max_hold_without_heartbeat(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    await _ready(space)
    lease = await space.pools.define("work", service="fake_tg", holder="w", **TIMING).acquire(max_hold=HOLD)
    await _pass(hub_under_test, 0.8)  # дедлайн минус safety_margin уже позади

    with pytest.raises(LeaseLost) as caught:
        lease.check()
    assert caught.value.reason == "max_hold"


async def test_abandoned_lease_is_closed_as_max_hold(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    await _ready(space, 2)
    pool = space.pools.define("work", service="fake_tg", holder="w", **TIMING)
    swept = await pool.acquire(max_hold=HOLD)
    reclaimed = await pool.acquire(
        max_hold=HOLD, among=[i for i in await _ids(space) if i != swept.identity_id]
    )
    await _pass(hub_under_test, 1.2)

    for renewal in await _renewals(hub_under_test, space, swept):  # срок вышел на своём дедлайне
        assert renewal == LeaseRenewal(None, LeaseEndReason.MAX_HOLD)

    successor = await pool.acquire(among=[reclaimed.identity_id])  # перезахват закрывает историю
    async with space._tx(own=True) as tx:
        assert await tx.expire_leases(10) == 1  # чистильщик закрывает вторую

    assert (await _history(space, reclaimed.lease_id)).end_reason == "max_hold"
    assert (await _history(space, swept.lease_id)).end_reason == "max_hold"
    await successor.release()


async def test_plain_expiry_stays_expired(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    await _ready(space)
    short_ttl = {**TIMING, "ttl": timedelta(seconds=0.6)}
    pool = space.pools.define("work", service="fake_tg", holder="w", **short_ttl)
    lease = await pool.acquire(max_hold=timedelta(seconds=30))
    await _pass(hub_under_test, 0.9)  # истёк ttl, до дедлайна далеко

    for renewal in await _renewals(hub_under_test, space, lease):
        assert renewal == LeaseRenewal(None, LeaseEndReason.EXPIRED)
    async with space._tx(own=True) as tx:
        assert await tx.expire_leases(10) == 1
    assert (await _history(space, lease.lease_id)).end_reason == "expired"


async def test_shared_lease_respects_the_deadline(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    hub_under_test.register_service(replace(FAKE_TG, name="fake_shared", lease_mode=LeaseMode.SHARED))
    await hub_under_test.start()  # строка сервиса в БД
    await _ready(space, service="fake_shared")
    lease = await space.pools.define("read", service="fake_shared", holder="r", **TIMING).acquire(
        max_hold=HOLD
    )
    assert lease.hold_deadline == lease.acquired_at + HOLD

    async with space._tx(own=True) as tx:
        live = await tx.extend_lease(lease._row, 60.0, LeaseCounters.empty(), shared=True)
    assert live.expires_at == lease.hold_deadline

    await _pass(hub_under_test, 1.2)
    async with space._tx(own=True) as tx:
        gone = await tx.extend_lease(lease._row, 60.0, LeaseCounters.empty(), shared=True)
    assert gone == LeaseRenewal(None, LeaseEndReason.MAX_HOLD)


async def test_deadline_is_visible_in_live_leases(space: SpaceHandle) -> None:
    await _ready(space)
    lease = await space.pools.define("work", service="fake_tg", holder="w", **TIMING).acquire(
        max_hold=timedelta(seconds=30)
    )

    dashboard = await space.admin.lease_dashboard()

    (live,) = dashboard.live
    assert live.hold_deadline == lease.hold_deadline
    await lease.release()


async def _ids(space: SpaceHandle) -> list[Any]:
    return [item.id for item in (await space.identities.list()).items]


async def _renewals(hub: IdentityHub, space: SpaceHandle, lease: Any) -> tuple[LeaseRenewal, LeaseRenewal]:
    batch = (await hub._store.renew_leases([lease._row.token], 60.0))[lease._row.token]
    async with space._tx(own=True) as tx:
        single = await tx.extend_lease(lease._row, 60.0, LeaseCounters.empty(), shared=False)
    return batch, single
