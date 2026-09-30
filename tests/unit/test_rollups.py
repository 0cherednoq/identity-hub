from __future__ import annotations

from datetime import UTC, datetime, timedelta

from identity_hub.clock import FakeClock
from identity_hub.crypto import StaticSecretsProvider
from identity_hub.hub import IdentityHub
from identity_hub.rollups import BucketSize, RollupDelta, bucket_start
from identity_hub.testing import MemoryStore
from tests.fixtures.services import FAKE_EXCHANGE


def _hub(start: datetime | None = None) -> tuple[IdentityHub, FakeClock, MemoryStore]:
    clock = FakeClock(start)
    store = MemoryStore(clock)
    hub = IdentityHub(
        secrets=StaticSecretsProvider({"test": b"0123456789abcdef0123456789abcdef"}, "test"),
        services=[FAKE_EXCHANGE],
        store=store,
        clock=clock,
        strict_events=True,
    )
    return hub, clock, store


async def _register(hub: IdentityHub, ref: str) -> None:
    await hub.space("analytics").register(
        service=FAKE_EXCHANGE,
        external_ref=ref,
        credentials={"email": ref, "password": "test"},
        session={"cookies": {"sid": "test"}},
        lifecycle="ready",
    )


async def test_rollup_retry_is_idempotent_and_series_fills_zeroes() -> None:
    start = datetime(2026, 1, 1, 12, tzinfo=UTC)
    hub, clock, store = _hub(start)
    await _register(hub, "first@example.test")
    first_batch = [event for event, _, _ in store._state.events]
    await hub.rollups.handle_events(first_batch)
    await hub.rollups.handle_events(first_batch)

    clock.advance(timedelta(minutes=2))
    previous_count = len(store._state.events)
    await _register(hub, "second@example.test")
    await hub.rollups.handle_events([event for event, _, _ in store._state.events[previous_count:]])

    points = await hub.rollups.series(
        "analytics",
        metric="identities.added",
        bucket_size=BucketSize.MINUTE,
        start=start,
        end=start + timedelta(minutes=3),
    )
    assert [point.value for point in points] == [1.0, 0.0, 1.0]


async def test_dispatcher_rolls_up_internal_lease_events_and_rebuilds_from_oracle() -> None:
    start = datetime(2026, 1, 1, 12, tzinfo=UTC)
    hub, _, store = _hub(start)
    await _register(hub, "lease@example.test")
    registered_id = store._state.events[0][0].identity_id
    assert registered_id is not None
    lease = await hub.space("analytics").pool("rollups", service=FAKE_EXCHANGE).acquire(among=[registered_id])
    await lease.release()
    await hub.dispatcher.drain()

    rows = await hub.rollups.rows(
        "analytics",
        metric="leases.acquired",
        bucket_size=BucketSize.HOUR,
        start=start,
        end=start + timedelta(hours=1),
    )
    assert sum(row.value for row in rows) == 1

    telemetry = await hub.rollups.rows(
        "analytics",
        metric="telemetry.lease_duration_seconds.count",
        bucket_size=BucketSize.MINUTE,
        start=start,
        end=start + timedelta(minutes=1),
    )
    assert sum(row.value for row in telemetry) == 1
    batch_id = next(iter(store._state.metric_batches))
    assert not await store.apply_metric_rollup_batch(batch_id, [RollupDelta(telemetry[0], additive=True)])
    assert (
        sum(
            row.value
            for row in await hub.rollups.rows(
                "analytics",
                metric="telemetry.lease_duration_seconds.count",
                bucket_size=BucketSize.MINUTE,
                start=start,
                end=start + timedelta(minutes=1),
            )
        )
        == 1
    )

    for key in list(store._state.rollups):
        store._state.rollups[key] = 999
    await hub.rollups.rebuild("analytics", start, start + timedelta(hours=1))
    rebuilt = await hub.rollups.rows(
        "analytics",
        metric="leases.acquired",
        bucket_size=BucketSize.HOUR,
        start=start,
        end=start + timedelta(hours=1),
    )
    assert sum(row.value for row in rebuilt) == 1


async def test_minute_retention_keeps_hour_and_day_rollups() -> None:
    start = datetime(2026, 1, 1, 12, tzinfo=UTC)
    hub, _, store = _hub(start)
    await _register(hub, "retention@example.test")
    await hub.rollups.handle_events([event for event, _, _ in store._state.events])

    assert await hub.rollups.purge_minutes(before=start + timedelta(minutes=1)) > 0
    assert not any(key.bucket_size is BucketSize.MINUTE for key in store._state.rollups)
    assert {key.bucket_size for key in store._state.rollups} == {
        BucketSize.HOUR,
        BucketSize.DAY,
    }


def test_daily_buckets_follow_space_timezone_across_dst() -> None:
    before = bucket_start(datetime(2026, 3, 28, 12, tzinfo=UTC), BucketSize.DAY, "Europe/Berlin")
    transition = bucket_start(datetime(2026, 3, 29, 12, tzinfo=UTC), BucketSize.DAY, "Europe/Berlin")
    after = bucket_start(datetime(2026, 3, 30, 12, tzinfo=UTC), BucketSize.DAY, "Europe/Berlin")
    assert transition - before == timedelta(hours=24)
    assert after - transition == timedelta(hours=23)
