from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from identity_hub.clock import FakeClock
from identity_hub.crypto import StaticSecretsProvider
from identity_hub.hub import IdentityHub, Retention
from identity_hub.testing import MemoryStore
from tests.fixtures.services import FAKE_EXCHANGE


def _hub(start: datetime, *, retention: timedelta = timedelta(days=365)) -> tuple[IdentityHub, MemoryStore]:
    clock = FakeClock(start)
    store = MemoryStore(clock)
    hub = IdentityHub(
        secrets=StaticSecretsProvider({"test": b"0123456789abcdef0123456789abcdef"}, "test"),
        services=[FAKE_EXCHANGE],
        store=store,
        clock=clock,
        retention=Retention(state_snapshots=retention),
        strict_events=True,
    )
    return hub, store


async def _register(hub: IdentityHub, ref: str) -> None:
    await hub.space("snapshots").register(
        service=FAKE_EXCHANGE,
        external_ref=ref,
        credentials={"email": ref, "password": "test"},
        session={"cookies": {"sid": "test"}},
        lifecycle="ready",
    )


async def test_snapshot_matches_current_slice_and_same_hour_is_idempotent() -> None:
    now = datetime(2026, 1, 1, 12, 25, tzinfo=UTC)
    hub, store = _hub(now)
    await _register(hub, "one@example.test")
    await _register(hub, "two@example.test")

    await asyncio.gather(hub.state_snapshots.capture(), hub.state_snapshots.capture())
    frames = await hub.state_snapshots.frames(
        "snapshots", start=now.replace(minute=0), end=now.replace(minute=0) + timedelta(hours=1)
    )
    assert len(frames) == 1
    assert frames[0].complete
    standing = [row for row in frames[0].rows if row.region == "standing"]
    assert sum(row.count for row in standing) == 2
    assert len(store._state.snapshot_coverage) == 1
    assert len(store._state.state_snapshots) == len(frames[0].rows)


async def test_empty_complete_frame_distinguishes_zero_from_missing_coverage() -> None:
    now = datetime(2026, 1, 1, 12, 25, tzinfo=UTC)
    hub, _ = _hub(now)
    await hub.space("snapshots").ref()
    await hub.state_snapshots.capture()

    frames = await hub.state_snapshots.frames(
        "snapshots", start=now.replace(minute=0), end=now.replace(minute=0) + timedelta(hours=2)
    )
    assert len(frames) == 1
    assert frames[0].complete and frames[0].rows == ()
    assert frames[0].taken_at == now.replace(minute=0)


async def test_backfill_only_captures_current_hour_and_retention_removes_old_coverage() -> None:
    now = datetime(2026, 1, 3, 12, 25, tzinfo=UTC)
    hub, _ = _hub(now, retention=timedelta(hours=24))
    await _register(hub, "backfill@example.test")
    await hub.state_snapshots.backfill_current()

    frames = await hub.state_snapshots.frames(
        "snapshots", start=now - timedelta(days=7), end=now + timedelta(hours=1)
    )
    assert len(frames) == 1
    assert frames[0].source == "backfill"
    assert frames[0].taken_at == now.replace(minute=0)
    assert await hub.state_snapshots.purge(before=now + timedelta(hours=1)) == 1
    assert not await hub.state_snapshots.frames(
        "snapshots", start=now - timedelta(days=7), end=now + timedelta(hours=1)
    )
