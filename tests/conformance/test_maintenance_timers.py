"""F3.6: таймеры обслуживания не голодают и одинаковы на обоих хранилищах (R2-F06, R2-P11)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import UUID

import pytest
from tests.fixtures.api import register

from identity_hub import IdentityHub, Override, SpaceHandle
from identity_hub.clock import FakeClock
from identity_hub.domain.regions import Command
from identity_hub.enums import Availability
from identity_hub.errors import LeaseUnavailable
from identity_hub.maintenance import MaintenanceSchedule

pytestmark = pytest.mark.conformance

# срок по реальному времени БД: запас на медленную машину, пока ставятся все состояния
SOON = timedelta(seconds=2)


async def _account(space: SpaceHandle, ref: str) -> UUID:
    reg = await register(
        space, service="fake_tg", external_ref=ref, lifecycle="ready", session={"auth_key": "k"}
    )
    return reg.identity_id


async def _standing(space: SpaceHandle, identity_id: UUID, state: str) -> None:
    async with space._tx(own=True) as tx:
        now = await tx.now()
    await space.identities.override(
        identity_id, Override("standing", "", state, reason="тест", actor="op", until=now + SOON)
    )


async def _availability(space: SpaceHandle, identity_id: UUID, state: Availability) -> None:
    async with space._tx(own=True) as tx:
        now = await tx.now()
    await space._command(identity_id, "availability", Command(state, actor="op", until=now + SOON))


async def _pass_time(hub: IdentityHub) -> None:
    if isinstance(hub.clock, FakeClock):
        hub.clock.advance(timedelta(hours=2))
    else:
        await asyncio.sleep(SOON.total_seconds() + 0.6)


async def _due(space: SpaceHandle, limit: int = 10) -> list[tuple[UUID, str]]:
    async with space._tx(own=True) as tx:
        return await tx.due_identity_timers(limit)


async def test_only_states_with_a_timer_transition_are_due(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    """В очередь таймеров попадают только состояния, у которых есть переход по истечении."""
    frozen, limited, paused, cooling, quarantined = [await _account(space, f"+{n}") for n in range(5)]
    await _standing(space, frozen, "frozen")  # `until` есть, перехода по таймеру нет
    await _standing(space, limited, "limited")
    await _availability(space, paused, Availability.PAUSED)
    await _availability(space, cooling, Availability.COOLING)
    await _availability(space, quarantined, Availability.QUARANTINED)  # снимается только командой
    assert await _due(space) == []  # срок ещё не вышел

    await _pass_time(hub_under_test)

    assert sorted(await _due(space)) == sorted(
        [(limited, "standing"), (paused, "availability"), (cooling, "availability")]
    )


async def test_rows_without_transition_do_not_starve_the_batch(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    """R2-F06 (S5): два `frozen` с истёкшим `until` не заслоняют `limited` при пачке 2."""
    hub_under_test.maintenance.schedule = MaintenanceSchedule(batch_size=2)
    for n in range(2):
        await _standing(space, await _account(space, f"+frozen{n}"), "frozen")
    limited = await _account(space, "+limited")
    await _standing(space, limited, "limited")
    await _pass_time(hub_under_test)

    report = await hub_under_test.maintenance.run_all()

    assert report.counts["region_timers"] == 1
    assert (await space.identities.get(limited)).standing == "good"


async def test_quarantine_with_expired_until_is_not_leased(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    """R2-P11: истёкший `until` лениво снимается только для `paused` и `cooling`."""
    quarantined = await _account(space, "+q")
    await _availability(space, quarantined, Availability.QUARANTINED)
    await _pass_time(hub_under_test)
    pool = space.pools.define("p", service="fake_tg")

    with pytest.raises(LeaseUnavailable):
        await pool.acquire()

    paused = await _account(space, "+p")
    await _availability(space, paused, Availability.PAUSED)
    await _pass_time(hub_under_test)
    lease = await pool.acquire()  # пауза истекла: аккаунт выдаётся и до прогона обслуживания
    assert lease.identity_id == paused
    await lease.release()
