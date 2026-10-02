"""F3.5: фоновая сверка не голодает и одинакова на обоих хранилищах (R2-F02, R2-F17, R2-F18, R2-P15)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from tests.fixtures.api import register
from tests.fixtures.services import FAKE_TG

from identity_hub import IdentityHub, ProbeResult, SpaceHandle, ValidationFailed
from identity_hub.domain.regions import Command
from identity_hub.enums import Availability

pytestmark = pytest.mark.conformance


class _Probes:
    """Проба `auth`: записывает, кого спрашивали; для ref из `broken` падает."""

    def __init__(self) -> None:
        self.asked: list[UUID] = []
        self.broken: set[UUID] = set()
        self.delay = 0.0

    async def auth(self, ctx: Any) -> ProbeResult | None:
        self.asked.append(ctx.identity_id)
        if self.delay:
            await asyncio.sleep(self.delay)
        if ctx.identity_id in self.broken:
            raise RuntimeError("platform 502")
        return ProbeResult("authenticated")

    async def standing(self, ctx: Any) -> ProbeResult | None:
        return ProbeResult("good")

    async def verification(self, ctx: Any, factor: str) -> ProbeResult | None:
        return None


async def _service(hub: IdentityHub, **options: Any) -> _Probes:
    probes = _Probes()
    hub.register_service(replace(FAKE_TG, name="probed", probes=probes, **options))
    await hub.start()
    return probes


async def _account(space: SpaceHandle, ref: str, lifecycle: str = "ready") -> UUID:
    reg = await register(
        space, service="probed", external_ref=ref, lifecycle=lifecycle, session={"auth_key": "k"}
    )
    return reg.identity_id


async def test_provisioning_accounts_do_not_starve_ready_ones(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    """R2-F02: после импорта (он создаёт `provisioning`) готовые аккаунты всё равно сверяются."""
    probes = await _service(hub_under_test)
    for n in range(3):
        await _account(space, f"+p{n}", "provisioning")
    ready = [await _account(space, f"+r{n}") for n in range(2)]

    report = await space.sync.refresh_many(service="probed", limit=3)

    assert sorted(report.probed) == sorted(ready)
    assert report.failed == {} and report.skipped_busy == []
    assert sorted(probes.asked) == sorted(ready)


async def test_retired_accounts_are_not_candidates(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    """R2-P15: `retired` исключён на обоих хранилищах."""
    await _service(hub_under_test)
    retired = await _account(space, "+old")
    live = await _account(space, "+live")
    await space.identities.retire(retired, actor="op", reason="списан")

    async with space._tx(own=True) as tx:
        candidates = await tx.stale_identities("probed", "auth", "default", 3600, 10)
    report = await space.sync.refresh_many(service="probed")

    assert candidates == [live]
    assert list(report.probed) == [live]


async def test_unavailable_accounts_are_reported_and_do_not_use_the_limit(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    """Аккаунты на паузе — `skipped_unavailable`, а не `failed`; лимит достаётся доступным."""
    await _service(hub_under_test)
    paused = [await _account(space, f"+paused{n}") for n in range(3)]
    for identity_id in paused:
        await space._command(identity_id, "availability", Command(Availability.PAUSED, actor="op"))
    ready = [await _account(space, f"+ok{n}") for n in range(2)]

    report = await space.sync.refresh_many(service="probed", limit=2)

    assert sorted(report.probed) == sorted(ready)
    assert sorted(report.skipped_unavailable) == sorted(paused)
    assert report.failed == {}


async def test_failing_probe_does_not_starve_the_queue(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    """Проба стабильно падает: попытка отмечена, следующие в очереди — те, кого ещё не спрашивали."""
    probes = await _service(hub_under_test)
    broken = [await _account(space, f"+bad{n}") for n in range(2)]
    probes.broken.update(broken)
    good = [await _account(space, f"+good{n}") for n in range(2)]

    first = await space.sync.refresh_many(service="probed", limit=2)

    assert sorted(first.probed) == sorted(good)  # лимит не потрачен на сбойные
    assert sorted(first.failed) == sorted(broken)
    assert all(reason.startswith("RuntimeError") for reason in first.failed.values())
    probes.asked.clear()
    second = await space.sync.refresh_many(service="probed", limit=2)
    assert sorted(second.failed) == sorted(broken) and second.probed == {}  # готовые уже сверены
    assert sorted(probes.asked) == sorted(broken)


async def test_stale_candidates_are_ordered_by_last_attempt(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    await _service(hub_under_test)
    first, second, third = [await _account(space, f"+{n}") for n in range(3)]

    async def candidates(limit: int = 10) -> list[UUID]:
        async with space._tx(own=True) as tx:
            return await tx.stale_identities("probed", "auth", "default", 3600, limit)

    assert await candidates() == [first, second, third]  # никого не спрашивали — по id
    async with space._tx(own=True) as tx:
        await tx.mark_probe_attempts([first])
    assert await candidates() == [second, third, first]
    assert await candidates(limit=2) == [second, third]
    async with space._tx(own=True) as tx:
        await tx.mark_probe_attempts([second, third])
    assert (await candidates())[0] == first


async def test_probe_timeout_bounds_a_hanging_probe(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    probes = await _service(hub_under_test, probe_timeout=timedelta(seconds=0.2))
    probes.delay = 30
    identity_id = await _account(space, "+slow")

    report = await asyncio.wait_for(space.sync.refresh_many(service="probed"), timeout=10)

    assert report.probed == {} and report.failed == {identity_id: "ProbeTimeout"}
    assert FAKE_TG.probe_timeout == timedelta(seconds=60)


async def test_verification_refresh_for_service_without_factors_is_empty(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    """R2-F18: `refresh_many(["verification"])` для сервиса без факторов — пустой отчёт, не IndexError."""
    await _service(hub_under_test)
    await _account(space, "+1")

    report = await space.sync.refresh_many(["verification"], service="probed")

    assert (report.probed, report.failed, report.skipped_busy) == ({}, {}, [])


def test_probe_result_requires_timezone_aware_times() -> None:
    """R2-F17: наивное время в `ProbeResult` отклоняется сразу, а не `TypeError` в машине регионов."""
    with pytest.raises(ValidationFailed):
        ProbeResult("good", observed_at=datetime(2030, 1, 1))
    with pytest.raises(ValidationFailed):
        ProbeResult("frozen", until=datetime(2030, 1, 1))
    from datetime import UTC

    aware = ProbeResult(
        "good", observed_at=datetime(2030, 1, 1, tzinfo=UTC), until=datetime(2031, 1, 1, tzinfo=UTC)
    )
    assert aware.observed_at is not None and aware.until is not None
