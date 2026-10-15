from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest

from identity_hub.clock import FakeClock
from identity_hub.crypto import StaticSecretsProvider
from identity_hub.enums import Severity
from identity_hub.errors import ValidationFailed
from identity_hub.events import Event
from identity_hub.hub import IdentityHub
from identity_hub.rollups import BucketSize, event_contributions
from identity_hub.testing import MemoryStore
from tests.fixtures.api import register
from tests.fixtures.services import FAKE_EXCHANGE


def _hub() -> tuple[IdentityHub, MemoryStore]:
    clock = FakeClock(datetime(2026, 2, 15, 12, tzinfo=UTC))
    store = MemoryStore(clock)
    hub = IdentityHub(
        secrets=StaticSecretsProvider({"test": b"0123456789abcdef0123456789abcdef"}, "test"),
        services=[FAKE_EXCHANGE],
        store=store,
        clock=clock,
        timezone="Europe/Berlin",
        strict_events=True,
    )
    return hub, store


def test_old_login_failure_event_falls_back_to_error_dimension() -> None:
    event = Event(
        id=1,
        event_id=uuid4(),
        type="identity.login.failed",
        severity=Severity.WARNING,
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        space="legacy",
        service="svc",
        identity_id=uuid4(),
        proxy_id=None,
        lease_id=None,
        sequence=1,
        actor="legacy",
        data={"error": "ConnectionError"},
    )
    assert ("logins.failed", (("code", "ConnectionError"),), 1.0) in event_contributions(event)


async def _golden(hub: IdentityHub, store: MemoryStore) -> tuple[datetime, datetime]:
    space = hub.space("analytics", create=True)
    ref = await space.ref()
    start = datetime(2026, 1, 5, tzinfo=UTC)
    end = start + timedelta(days=7)
    first, second, previous = uuid4(), uuid4(), uuid4()
    specs = [
        (start - timedelta(days=1), "identity.registered", previous, {}),
        (start, "identity.registered", first, {"labels": {"batch": "A"}, "tags": ["farm"]}),
        (start + timedelta(seconds=1), "identity.registered", second, {"labels": {"batch": "A"}}),
        (start + timedelta(seconds=10), "identity.auth.changed", first, {"to": "authenticated"}),
        (start + timedelta(seconds=21), "identity.auth.changed", second, {"to": "authenticated"}),
        (
            start + timedelta(minutes=1),
            "identity.verification.changed",
            first,
            {"key": "email", "to": "pending", "attempts": 1},
        ),
        (
            start + timedelta(minutes=2),
            "identity.verification.changed",
            first,
            {"key": "email", "to": "verified", "attempts": 2},
        ),
        (start + timedelta(minutes=2), "identity.tier.reached", first, {"tier": "basic", "factor": "email"}),
        (start + timedelta(hours=12), "identity.standing.changed", second, {"from": "good", "to": "banned"}),
        (
            start + timedelta(hours=13),
            "operation.failed",
            second,
            {"kind": "send", "blame": "proxy", "code": "timeout"},
        ),
        (start + timedelta(hours=14), "proxy.status.changed", None, {"to": "quarantined"}),
        (start + timedelta(hours=15), "proxy.status.changed", None, {"to": "dead"}),
        (start + timedelta(hours=16), "proxy.status.changed", None, {"to": "alive"}),
    ]
    events = []
    for index, (occurred_at, event_type, identity_id, data) in enumerate(specs, 1):
        event = Event(
            id=index,
            event_id=uuid4(),
            type=event_type,
            severity=Severity.INFO,
            occurred_at=occurred_at,
            space="analytics",
            service=FAKE_EXCHANGE.name,
            identity_id=identity_id,
            proxy_id=None,
            lease_id=None,
            sequence=index,
            actor="golden",
            data=data,
        )
        store._state.events.append((event, ref.id, not event_type.startswith(("lease.", "operation."))))
        events.append(event)
    store._state.next_event_id = len(events) + 1
    await hub._rollups.handle_events(events)
    await hub._state_snapshots.capture(at=hub.clock.now())
    return start, end


async def test_golden_series_funnel_time_to_cohorts_ban_rate_and_proxy_quality() -> None:
    hub, store = _hub()
    start, end = await _golden(hub, store)
    space = hub.space("analytics", create=True)

    series = await space.analytics.series(
        "identities.added", start=start, end=end, bucket_size=BucketSize.DAY
    )
    assert sum(point.value for point in series.groups[0].points) == 2
    assert series.groups[0].previous_total == 1
    assert (
        series.cache_tag
        == (
            await space.analytics.series("identities.added", start=start, end=end, bucket_size=BucketSize.DAY)
        ).cache_tag
    )

    funnel = await space.analytics.funnel(start=start, end=end)
    assert [(stage.key, stage.count) for stage in funnel.stages] == [
        ("added", 2),
        ("authenticated", 2),
        ("tier:basic", 1),
    ]
    time_to = await space.analytics.time_to("added_to_authenticated", start=start, end=end)
    assert time_to.samples == 2
    assert time_to.median_seconds == 15
    assert time_to.p90_seconds == 19
    verification = await space.analytics.time_to("verification:email", start=start, end=end)
    assert (verification.samples, verification.median_seconds, verification.average_attempts) == (1, 60, 2)

    cohorts = await space.analytics.cohorts(start=start, end=end)
    assert cohorts.coverage == "partial"
    assert cohorts.rows[0].cells[0].share == 0.5
    rates = await space.analytics.ban_rate(start=start, end=end, group_by="batch")
    assert rates.items[0].key == "A" and rates.items[0].rate == 0.5
    proxy = await space.analytics.proxy_quality(start=start, end=end)
    assert (proxy.failures, proxy.quarantined, proxy.dead, proxy.recovered) == (1, 1, 1, 1)


async def test_analytics_rejects_unknown_dimensions_and_caps_points() -> None:
    hub, store = _hub()
    start, end = await _golden(hub, store)
    space = hub.space("analytics", create=True)
    with pytest.raises(ValidationFailed):
        await space.analytics.series(
            "identities.added",
            start=start,
            end=end,
            bucket_size=BucketSize.DAY,
            group_by="identity_id",
        )
    with pytest.raises(ValidationFailed):
        await space.analytics.series(
            "identities.added",
            start=start,
            end=start + timedelta(days=1),
            bucket_size=BucketSize.MINUTE,
        )
    with pytest.raises(ValidationFailed):
        await space.analytics.series(
            "identities.added",
            start=start,
            end=end,
            bucket_size="2h",  # type: ignore[arg-type]
        )

    ref = await space.ref()
    events = []
    for index in range(51):
        event = Event(
            id=100 + index,
            event_id=uuid4(),
            type="identity.login.failed",
            severity=Severity.WARNING,
            occurred_at=start + timedelta(minutes=index),
            space="analytics",
            service=FAKE_EXCHANGE.name,
            identity_id=uuid4(),
            proxy_id=None,
            lease_id=None,
            sequence=100 + index,
            actor="group-limit",
            data={"code": f"code-{index}"},
        )
        store._state.events.append((event, ref.id, True))
        events.append(event)
    await hub._rollups.handle_events(events)
    with pytest.raises(ValidationFailed):
        await space.analytics.series(
            "logins.failed",
            start=start,
            end=end,
            bucket_size=BucketSize.DAY,
            group_by="code",
        )


async def test_analytics_daily_series_uses_wall_clock_days_across_dst() -> None:
    hub, store = _hub()
    space = hub.space("analytics", create=True)
    ref = await space.ref()
    zone = ZoneInfo("Europe/Berlin")
    local_start = datetime(2026, 3, 28, tzinfo=zone)
    events: list[Event] = []
    for index, local_time in enumerate(
        (
            local_start + timedelta(hours=12),
            local_start + timedelta(days=1, hours=12),
            local_start + timedelta(days=2, hours=12),
        ),
        1,
    ):
        event = Event(
            id=index,
            event_id=uuid4(),
            type="identity.registered",
            severity=Severity.INFO,
            occurred_at=local_time.astimezone(UTC),
            space="analytics",
            service=FAKE_EXCHANGE.name,
            identity_id=uuid4(),
            proxy_id=None,
            lease_id=None,
            sequence=index,
            actor="dst-golden",
            data={},
        )
        store._state.events.append((event, ref.id, True))
        events.append(event)
    await hub._rollups.handle_events(events)

    series = await space.analytics.series(
        "identities.added",
        start=local_start,
        end=local_start + timedelta(days=3),
        bucket_size=BucketSize.DAY,
    )
    points = series.groups[0].points
    assert [point.value for point in points] == [1, 1, 1]
    assert points[1].at - points[0].at == timedelta(hours=24)
    assert points[2].at - points[1].at == timedelta(hours=23)


async def test_utilization_heatmap_uses_space_timezone() -> None:
    hub, _ = _hub()
    space = hub.space("analytics", create=True)
    registered = await register(
        space,
        service=FAKE_EXCHANGE,
        external_ref="lease@example.test",
        credentials={"email": "lease@example.test", "password": "test"},
        session={"cookies": {"sid": "test"}},
        lifecycle="ready",
        auth="authenticated",
    )
    start = hub.clock.now()
    lease = await space.pools.define("analytics", service=FAKE_EXCHANGE).acquire(
        among=[registered.identity_id]
    )
    assert isinstance(registered.identity_id, UUID)
    hub.clock.advance(60)  # type: ignore[attr-defined]
    await lease.release()
    report = await space.analytics.utilization(start=start, end=hub.clock.now())
    assert report.leased_seconds == 60
    assert report.heatmap[0].leases == 1
