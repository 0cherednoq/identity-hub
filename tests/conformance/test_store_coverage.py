"""F5.3b (R2-P23): методы Store, которые общие тесты раньше не вызывали ни на одном из бэкендов."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import SecretStr
from tests.fixtures.api import register

from identity_hub import IdentityHub, SpaceHandle
from identity_hub.errors import ConcurrentModification, NotFound
from identity_hub.events import EventDraft
from identity_hub.store.base import NOTIFY_CHANNEL, QuotaCharge

pytestmark = pytest.mark.conformance


async def _account(space: SpaceHandle, ref: str = "+100") -> object:
    reg = await register(
        space, service="fake_tg", external_ref=ref, lifecycle="ready", session={"auth_key": "k"}
    )
    return reg.identity_id


async def test_get_space_finds_existing_and_skips_unknown(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    ref = await space.ref()

    found = await hub_under_test._store.get_space(space.slug)

    assert found is not None and found.id == ref.id
    assert await hub_under_test._store.get_space(f"absent-{uuid4().hex[:6]}") is None
    assert await hub_under_test.get_space(f"absent-{uuid4().hex[:6]}") is None


async def test_listen_signals_committed_event(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    async with hub_under_test._store.listen(NOTIFY_CHANNEL) as signal:
        assert signal is not None  # и MemoryStore, и прямой PostgreSQL доставляют уведомления
        async with space._tx(own=True) as tx:
            await space._emit(tx, EventDraft("identity.updated", actor="test"))
        await asyncio.wait_for(signal.wait(), timeout=10)


async def test_pacing_counter_reports_threshold(space: SpaceHandle) -> None:
    identity_id = await _account(space)
    charge = QuotaCharge("__pacing__", 3600, datetime(2020, 1, 1, tzinfo=UTC), 2)

    async with space._tx(own=True) as tx:
        assert await tx.bump_pacing(identity_id, charge) is False
        assert await tx.bump_pacing(identity_id, charge) is True  # порог: нужна пауза, счётчик обнулён
        assert await tx.bump_pacing(identity_id, charge) is False


async def test_pool_capacity_counts_live_leases(space: SpaceHandle) -> None:
    await _account(space, "+1")
    await _account(space, "+2")
    pool = space.pools.define("p", service="fake_tg")

    async with pool.lease():
        capacity = await space.admin.pool_capacity(pool)

    assert (capacity.eligible, capacity.leased, capacity.free) == (2, 1, 1)


async def test_event_detail_reads_one_event(space: SpaceHandle) -> None:
    async with space._tx(own=True) as tx:
        await space._emit(tx, EventDraft("identity.updated", actor="test", data={"n": 1}))
    page = await space.admin.event_page()
    event_id = page.items[0].event_id

    detail = await space.admin.event_detail(event_id)

    assert detail.event_id == event_id and detail.type == "identity.updated"
    with pytest.raises(NotFound):
        await space.admin.event_detail(uuid4())


async def test_lease_log_rows_are_stored(space: SpaceHandle) -> None:
    identity_id = await _account(space)
    lease = await space.pools.define("p", service="fake_tg").acquire()
    async with space._tx(own=True) as tx:
        now = await tx.now()
        await tx.insert_lease_logs(
            [
                {
                    "lease_id": lease.lease_id,
                    "identity_id": identity_id,
                    "ts": now,
                    "level": 30,
                    "message": "проверка",
                    "fields": {"step": 1},
                }
            ]
        )
    await lease.release()

    # чтения логов аренды в API нет: запись видна по чистке, которая возвращает число строк
    store = space.hub._store
    assert await store.cleanup_maintenance({"lease_logs": timedelta(days=1)}, frozenset({"lease_logs"})) == {
        "lease_logs": 0
    }
    assert await store.cleanup_maintenance(
        {"lease_logs": timedelta(hours=-1)}, frozenset({"lease_logs"})
    ) == {"lease_logs": 1}


async def test_admin_lists_import_runs_and_provider_operations(space: SpaceHandle) -> None:
    await space.ref()

    assert await space.admin.import_runs() == ()
    assert await space.admin.provider_operations() == ()


async def test_identity_revision_lock_detects_concurrent_change(space: SpaceHandle) -> None:
    identity_id = await _account(space)
    async with space._tx(own=True) as tx:
        revision = (await tx.get_identity(identity_id)).event_seq  # type: ignore[union-attr]
        assert await tx.lock_identity_revision(identity_id, revision) == revision
        with pytest.raises(ConcurrentModification):
            await tx.lock_identity_revision(identity_id, revision + 1)
        with pytest.raises(NotFound):
            await tx.lock_identity_revision(uuid4(), 0)


async def test_webhook_endpoint_update_rotate_and_delete(space: SpaceHandle) -> None:
    endpoint = await space.webhooks.create(
        url="https://hooks.example.test/a", secret=SecretStr("s1"), event_types=["identity.updated"]
    )

    await space.webhooks.update(endpoint.id, url="https://hooks.example.test/b", event_types=["identity.*"])
    await space.webhooks.rotate_secret(endpoint.id, SecretStr("s2"), overlap=timedelta(minutes=5))
    async with space._tx(own=True) as tx:
        row = await tx.get_webhook_endpoint(endpoint.id)
    assert row is not None and row.event_types == ["identity.*"] and row.url.endswith("/b")

    await space.webhooks.delete(endpoint.id)

    async with space._tx(own=True) as tx:
        assert await tx.get_webhook_endpoint(endpoint.id) is None
    with pytest.raises(NotFound):
        await space.webhooks.delete(endpoint.id)


async def test_notification_channel_rule_lifecycle(space: SpaceHandle) -> None:
    channel = await space.notifications.create_telegram_channel(
        "ops", bot_token=SecretStr("123:token"), chat_id=-100
    )
    rule = await space.notifications.create_rule(
        name="все", channel_id=channel.id, event_types=["identity.*"], created_by="test"
    )
    async with space._tx(own=True) as tx:
        secret = await tx.get_notification_channel_secret(channel.id)
        assert secret is not None
        assert await tx.get_notification_channel_secret(uuid4()) is None

    with pytest.raises(NotFound):
        await space.notifications.retry(10**9)
    await space.notifications.delete_rule(rule.id)
    assert await space.notifications.rules() == []
    await space.notifications.delete_channel(channel.id)
    assert await space.notifications.channels() == []
    with pytest.raises(NotFound):
        await space.notifications.delete_channel(channel.id)
