"""F4.1b: чистка по срокам хранения одинакова на обоих хранилищах (R2-D10)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from tests.fixtures.api import register

from identity_hub import IdentityHub, Retention, SpaceHandle
from identity_hub._ids import new_id
from identity_hub.crypto.envelope import EncryptedPayload
from identity_hub.events import EventDraft
from identity_hub.rollups import BucketSize, RollupKey, RollupRow

pytestmark = pytest.mark.conformance

OLD = timedelta(hours=-1)  # граница в будущем: всё уже записанное считается старше срока
KEEP = timedelta(days=3650)
BUCKET = datetime(2025, 3, 1, tzinfo=UTC)
NEW = frozenset({"metric_batches", "closed_challenges", "rollup_hours", "rollup_days"})


def _retention(default: timedelta, **overrides: timedelta) -> dict[str, timedelta]:
    names = (
        "events",
        "lease_history",
        "lease_logs",
        "deliveries",
        "operation_keys",
        "import_runs",
        "provider_operations",
        *NEW,
    )
    return {name: default for name in names} | overrides


async def _challenge(space: SpaceHandle, ref: str, *, resolved: bool) -> UUID:
    reg = await register(space, service="fake_tg", external_ref=ref, lifecycle="ready")
    challenge_id = new_id()
    async with space._tx(own=True) as tx:
        assert await tx.open_challenge(
            challenge_id,
            reg.identity_id,
            "sms_code",
            "код из SMS",
            EncryptedPayload("k1", b"", b"", b""),
            None,
        )
        if resolved:
            assert await tx.resolve_challenge(challenge_id)
    return challenge_id


async def _rollups(hub: IdentityHub, space: SpaceHandle) -> None:
    ref = await space.ref()
    await hub._store.replace_rollups(
        [RollupRow(RollupKey(ref.id, "", "leases.acquired", (), BUCKET, size), 1.0) for size in BucketSize]
    )


async def _rollup_sizes(hub: IdentityHub, space: SpaceHandle) -> list[BucketSize]:
    found = []
    for size in BucketSize:
        rows = await hub._store.read_rollups(
            space.slug,
            metric="leases.acquired",
            bucket_size=size,
            start=BUCKET - timedelta(days=2),
            end=BUCKET + timedelta(days=2),
            service=None,
            dims=None,
        )
        found.extend(size for _row in rows)
    return found


def test_retention_has_terms_for_new_categories() -> None:
    retention = Retention()
    assert retention.metric_batches == timedelta(days=7)
    assert retention.closed_challenges == timedelta(days=30)
    assert retention.rollup_hours == timedelta(days=90)
    assert retention.rollup_days == timedelta(days=730)


async def test_new_categories_are_cleaned(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    """Квитанции метрик, закрытые вопросы и крупные корзины больше не копятся вечно."""
    store = hub_under_test._store
    closed = await _challenge(space, "closed", resolved=True)
    still_open = await _challenge(space, "open", resolved=False)
    batch_id = uuid4()
    assert await store.apply_metric_rollup_batch(batch_id, [])
    await _rollups(hub_under_test, space)

    kept = await store.cleanup_maintenance(_retention(KEEP), NEW)

    assert kept == dict.fromkeys(NEW, 0)
    assert await store.apply_metric_rollup_batch(batch_id, []) is False  # квитанция на месте

    counts = await store.cleanup_maintenance(_retention(OLD), NEW)

    assert counts == dict.fromkeys(NEW, 1)
    async with space._tx(own=True) as tx:
        assert await tx.get_challenge(closed) is None
        assert await tx.get_challenge(still_open) is not None  # открытый вопрос не удаляется
    assert await store.apply_metric_rollup_batch(batch_id, []) is True
    assert await _rollup_sizes(hub_under_test, space) == [BucketSize.MINUTE]


async def test_deleted_events_take_their_deliveries(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    """Событие удалено по сроку — его доставки удаляются вместе с ним, а не крутятся в очереди."""
    endpoint = await space.webhooks.create(
        url="https://hooks.example.test/retention", secret=SecretStr("s"), event_types=["identity.updated"]
    )
    for n in range(2):
        async with space._tx(own=True) as tx:
            await space._emit(tx, EventDraft("identity.updated", actor="test", data={"n": n}))
    store = hub_under_test._store
    await store.process_events("webhooks", hub_under_test._webhook_dispatcher.handle_events, limit=100)
    assert len(await space.webhooks.deliveries(endpoint.id)) == 2

    counts = await store.cleanup_maintenance(_retention(KEEP, events=OLD), frozenset({"events"}))

    assert counts["events"] >= 2
    assert counts["webhook_deliveries"] == 2
    assert await space.webhooks.deliveries(endpoint.id) == []


async def test_cleanup_in_small_batches_removes_everything(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    store = hub_under_test._store
    for _ in range(5):
        assert await store.apply_metric_rollup_batch(uuid4(), [])

    counts = await store.cleanup_maintenance(_retention(OLD), frozenset({"metric_batches"}), batch_size=2)

    assert counts == {"metric_batches": 5}


async def test_daily_maintenance_runs_new_categories(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    await space.ref()
    report = await hub_under_test.maintenance.run_all()

    assert report.counts.keys() >= NEW
