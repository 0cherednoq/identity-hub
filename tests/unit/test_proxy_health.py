"""F3.7a: здоровье прокси — простой провайдера, сбой чекера, закрепление (R2-F04, R2-F14)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from identity_hub import IdentityHub, Override, ProxyStatus, SpaceHandle, StaticSecretsProvider
from identity_hub.clock import FakeClock
from identity_hub.domain.region_specs import STANDING
from identity_hub.domain.regions import Outcome, RegionState, Timer, apply
from identity_hub.proxies import ProxyHealthPolicy
from identity_hub.read.events import EventFilters
from identity_hub.testing import MemoryStore, make_identity, make_service

pytestmark = pytest.mark.unit


async def _space(**policy: Any) -> tuple[IdentityHub, FakeClock, SpaceHandle]:
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    hub = IdentityHub(
        store=MemoryStore(),
        secrets=StaticSecretsProvider({"k": bytes(range(32))}, "k"),
        services=[make_service("svc")],
        clock=clock,
        proxy_health=ProxyHealthPolicy(jitter=0.0, **policy),
    )
    await hub.start()
    return hub, clock, hub.space("s", "S", create=True)


async def _quarantined(space: SpaceHandle, count: int, group: str = "g") -> list[UUID]:
    ids = []
    for n in range(count):
        proxy, _ = await space.proxies.ensure(f"http://u:p@10.0.{len(group)}.{n}:80", group=group)
        for _ in range(3):
            await space.proxies.report_failure(proxy.id, actor="test")
        ids.append(proxy.id)
    return ids


async def _statuses(space: SpaceHandle, ids: list[UUID]) -> list[str]:
    return [str((await space.proxies.get(proxy_id)).status) for proxy_id in ids]


async def _events(space: SpaceHandle, type_: str) -> list[Any]:
    page = await space.admin.event_page(filters=EventFilters(type=type_))
    return list(page.items)


async def down(url: str) -> bool:
    return False


async def up(url: str) -> bool:
    return True


async def test_provider_outage_does_not_kill_the_whole_group() -> None:
    """R2-F04 (P1): полчаса простоя провайдера не переводят группу в `dead`; после возврата — `alive`."""
    _hub, clock, space = await _space()
    ids = await _quarantined(space, 3)
    assert await _statuses(space, ids) == ["quarantined"] * 3

    for _ in range(40):  # 40 минут все прокси группы не отвечают
        clock.advance(60)
        await space.proxies.recheck_due(down)

    assert await _statuses(space, ids) == ["quarantined"] * 3
    failed = await _events(space, "proxy.checker.failed")
    assert failed and {item.data["reason"] for item in failed} == {"all_failed"}
    assert all(item.data["pool"] == "g" and item.data["proxies"] == 3 for item in failed)

    for _ in range(3):  # провайдер вернулся: выход из карантина — две успешные проверки подряд
        clock.advance(timedelta(minutes=5))
        await space.proxies.recheck_due(up)
    assert await _statuses(space, ids) == ["alive"] * 3


async def test_single_failing_proxy_still_goes_dead_and_is_rechecked_later() -> None:
    """Отказ одного прокси при живых соседях засчитывается; `dead` перепроверяется раз в `max_backoff`."""
    _hub, clock, space = await _space(dead_after=2)
    bad, good = await _quarantined(space, 2)

    async def one_down(url: str) -> bool:
        return "10.0.1.0" not in url

    for _ in range(6):
        clock.advance(timedelta(minutes=10))
        await space.proxies.recheck_due(one_down)
    assert await _statuses(space, [bad, good]) == ["dead", "alive"]
    assert await _events(space, "proxy.checker.failed") == []

    clock.advance(timedelta(minutes=5))
    assert (await space.proxies.recheck_due(up)).statuses == {}  # dead ждёт max_backoff (1 час)
    for _ in range(3):
        clock.advance(timedelta(minutes=61))
        await space.proxies.recheck_due(up)
    assert await _statuses(space, [bad]) == ["alive"]  # ожил без ручного вмешательства


async def test_checker_exception_is_not_a_proxy_failure() -> None:
    _hub, clock, space = await _space(dead_after=2)
    (proxy_id,) = await _quarantined(space, 1)

    async def broken(url: str) -> bool:
        raise RuntimeError(f"control URL is down for {url}")

    for _ in range(6):
        clock.advance(timedelta(minutes=10))
        await space.proxies.recheck_due(broken)

    proxy = await space.proxies.get(proxy_id)
    assert (str(proxy.status), proxy.fail_streak) == ("quarantined", 0)
    failed = await _events(space, "proxy.checker.failed")
    assert failed and {item.data["reason"] for item in failed} == {"checker_error"}
    assert "u:p@" not in str([dict(item.data) for item in failed])


async def test_checker_timeout_counts_as_proxy_failure() -> None:
    _hub, clock, space = await _space(dead_after=1, check_timeout=0.05)
    (proxy_id,) = await _quarantined(space, 1)

    async def hangs(url: str) -> bool:
        await asyncio.sleep(30)
        return True

    clock.advance(timedelta(minutes=10))
    result = (await asyncio.wait_for(space.proxies.recheck_due(hangs), timeout=5)).statuses

    assert result == {proxy_id: ProxyStatus.DEAD}


async def test_recheck_due_runs_checks_in_parallel_with_a_limit() -> None:
    _hub, clock, space = await _space(check_concurrency=3)
    ids = await _quarantined(space, 7)
    active = peak = 0

    async def slow(url: str) -> bool:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return True

    clock.advance(timedelta(minutes=10))
    result = (await space.proxies.recheck_due(slow)).statuses

    assert set(result) == set(ids) and peak == 3


async def test_recheck_group_revives_dead_proxies_without_waiting() -> None:
    """Провайдер вернулся: одна команда перепроверяет все недоступные прокси группы."""
    _hub, _clock, space = await _space()
    ids = await _quarantined(space, 3)
    other = await _quarantined(space, 1, group="other")
    for proxy_id in ids[:2]:
        await space.proxies.override(proxy_id, "dead", reason="провайдер лёг", actor="op")

    result = (await space.proxies.recheck_group("g", up, actor="op")).statuses

    assert result == dict.fromkeys(ids, ProxyStatus.ALIVE)
    assert await _statuses(space, other) == ["quarantined"]  # другая группа не тронута


async def test_pinned_proxy_is_not_changed_by_scheduled_rechecks() -> None:
    """R2-F14 (P2): закреплённый прокси не уходит в `dead` и не перепроверяется по расписанию."""
    _hub, clock, space = await _space(dead_after=2)
    proxy, _ = await space.proxies.ensure("http://u:p@10.0.0.1:80", group="g")
    await space.proxies.override(
        proxy.id, "quarantined", reason="hold", actor="op", pin_for=timedelta(days=7)
    )
    calls = 0

    async def counting_down(url: str) -> bool:
        nonlocal calls
        calls += 1
        return False

    for _ in range(8):
        clock.advance(timedelta(hours=1))
        assert (await space.proxies.recheck_due(counting_down)).statuses == {}

    pinned = await space.proxies.get(proxy.id)
    assert (str(pinned.status), calls) == ("quarantined", 0)

    clock.advance(timedelta(days=7))  # закрепление истекло: обычные правила возвращаются
    for _ in range(3):
        clock.advance(timedelta(hours=2))
        await space.proxies.recheck_due(counting_down)
    assert str((await space.proxies.get(proxy.id)).status) == "dead"


def test_backoff_delay_has_jitter_within_bounds() -> None:
    policy = ProxyHealthPolicy(jitter=0.2)
    base = policy.backoff(2)
    delays = {policy.delay(2) for _ in range(50)}
    assert len(delays) > 1 and all(base <= delay <= base * 1.2 for delay in delays)
    assert all(policy.max_backoff <= policy.dead_delay() <= policy.max_backoff * 1.2 for _ in range(20))
    assert ProxyHealthPolicy(jitter=0.0).delay(2) == base


def test_timer_does_not_fire_on_pinned_region() -> None:
    """R2-F14: таймер `limited → good` не срабатывает, пока регион закреплён."""
    now = datetime(2026, 1, 2, tzinfo=UTC)
    limited = RegionState(state="limited", changed_at=now - timedelta(days=1), until=now - timedelta(hours=1))

    pinned = apply(
        STANDING, RegionState(**{**_fields(limited), "pinned_until": now + timedelta(days=1)}), Timer(), now
    )
    assert (pinned.outcome, pinned.state.state) == (Outcome.PINNED, "limited")

    expired_pin = apply(
        STANDING,
        RegionState(**{**_fields(limited), "pinned_until": now - timedelta(minutes=1)}),
        Timer(),
        now,
    )
    assert expired_pin.state.state == "good"
    assert apply(STANDING, limited, Timer(), now).state.state == "good"


def _fields(state: RegionState) -> dict[str, Any]:
    from dataclasses import fields

    return {item.name: getattr(state, item.name) for item in fields(state)}


async def test_pinned_standing_is_skipped_by_maintenance_until_pin_expires() -> None:
    hub, clock, space = await _space()
    identity_id = await make_identity(space, service="svc")
    await space.identities.override(
        identity_id,
        Override(
            "standing",
            "",
            "limited",
            reason="держим",
            actor="op",
            until=clock.now() + timedelta(minutes=5),
            pin_for=timedelta(days=1),
        ),
    )
    clock.advance(timedelta(hours=1))

    report = await hub.maintenance.run_all()
    assert report.counts["region_timers"] == 0
    assert (await space.identities.get(identity_id)).standing == "limited"
    async with space._tx(own=True) as tx:
        assert await tx.due_identity_timers(10) == []  # закреплённая строка не занимает очередь

    clock.advance(timedelta(days=1))
    await hub.maintenance.run_all()
    assert (await space.identities.get(identity_id)).standing == "good"
