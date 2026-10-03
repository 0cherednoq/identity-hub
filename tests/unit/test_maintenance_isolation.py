"""F3.6: один сбойный аккаунт или группа не останавливает обслуживание (R2-F06)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from identity_hub import IdentityHub, Override, StaticSecretsProvider, VerificationSpec
from identity_hub.clock import FakeClock
from identity_hub.metrics import RecordingMetrics
from identity_hub.testing import MemoryStore, make_identity, make_service

pytestmark = pytest.mark.unit

SECRETS = StaticSecretsProvider({"k": bytes(range(32))}, "k")


def _hub(store: MemoryStore, clock: FakeClock, services: list[Any], metrics: RecordingMetrics) -> IdentityHub:
    return IdentityHub(store=store, secrets=SECRETS, services=services, clock=clock, metrics=metrics)


async def test_identity_of_unregistered_service_does_not_stop_maintenance() -> None:
    """S7: сервис убран из кода — его аккаунт пропускается, таймеры остальных срабатывают."""
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    store = MemoryStore()
    kept = make_service("kept", verifications=(VerificationSpec("kyc"),))
    removed = make_service("removed")
    hub = _hub(store, clock, [kept, removed], RecordingMetrics())
    await hub.start()
    space = hub.space("s", "S", create=True)
    of_kept = await make_identity(space, service=kept)
    of_removed = await make_identity(space, service=removed)
    now = clock.now()
    # аккаунт убранного сервиса — первый в очереди таймеров
    await space.identities.override(
        of_removed,
        Override("standing", "", "limited", reason="x", actor="op", until=now + timedelta(minutes=1)),
    )
    await space.identities.override(
        of_kept, Override("standing", "", "limited", reason="x", actor="op", until=now + timedelta(minutes=5))
    )
    clock.advance(timedelta(hours=1))

    metrics = RecordingMetrics()
    restarted = _hub(store, clock, [kept], metrics)  # процесс перезапущен без сервиса `removed`
    await restarted.start()
    report = await restarted.maintenance.run_all()

    assert report.counts["region_timers"] == 1
    assert (await restarted.space("s", create=True).identities.get(of_kept)).standing == "good"
    skipped = [
        event
        for event in metrics.events
        if event.name == "identity_hub_failures_total" and event.labels.get("scope") == "maintenance"
    ]
    assert [(event.labels["reason"], event.labels["service"]) for event in skipped] == [
        ("unknown_service", "removed")
    ]
    assert restarted.maintenance.retrying == set()


async def test_failing_group_does_not_block_other_groups_in_run_due() -> None:
    """Сбой одной группы задач не мешает остальным в том же тике и повторяется на следующем."""
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    hub = _hub(MemoryStore(), clock, [make_service("svc")], RecordingMetrics())
    await hub.start()
    space = hub.space("s", "S", create=True)
    identity_id = await make_identity(space, service="svc")
    pool = space.pools.define(
        "p",
        service="svc",
        ttl=timedelta(seconds=3),
        heartbeat=timedelta(seconds=1),
        safety_margin=timedelta(seconds=1),
    )
    abandoned = await pool.acquire()
    clock.advance(10)
    calls = {"normalize": 0}
    original = hub.maintenance._normalize

    async def broken() -> Any:
        calls["normalize"] += 1
        if calls["normalize"] == 1:
            raise RuntimeError("database hiccup")
        return await original()

    hub.maintenance._normalize = broken  # type: ignore[method-assign]

    assert await hub.maintenance.run_due() >= 1  # группа leases отработала, несмотря на сбой normalize
    (history,) = await space.admin.lease_history(identity_id)
    assert history.lease_id == abandoned.lease_id and history.end_reason == "expired"
    assert hub.maintenance.retrying == {"normalize"}

    await hub.maintenance.run_due()  # сбойная группа повторяется сразу, без ожидания интервала
    assert calls["normalize"] == 2 and hub.maintenance.retrying == set()
