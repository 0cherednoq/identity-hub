"""PA4.2: один набор событий на переход, атомарный outbox и безопасные данные."""

from collections import Counter
from datetime import timedelta

import pytest
from tests.conformance.test_proxy_allocation_confirm import _setup as _configure
from tests.conformance.test_proxy_allocation_recovery import _expire, _setup
from tests.event_data_contract import documented
from tests.fixtures.api import register

from identity_hub import LeaseUnavailable, ProxyAssignmentConflict, ProxyBindingPolicy, ProxyPolicy, RegionIn
from identity_hub.metrics import RecordingMetrics
from identity_hub.proxy_allocator import confirm
from identity_hub.read.events import EventFilters
from identity_hub.rollups import BucketSize

pytestmark = pytest.mark.conformance


async def _events(space):
    page = await space.admin.event_page(filters=EventFilters(group="proxy"), limit=200)
    return sorted(
        (event for event in page.items if event.type.startswith(("proxy.reservation.", "proxy.allocation."))),
        key=lambda event: event.event_id,
    )


async def test_reservation_confirm_replay_and_release_have_one_transition_each(space):
    _, identity, pool = await _configure(space)
    lease = await pool.acquire(among=[identity], idempotency_key="check")
    replay = await pool.acquire(among=[identity], idempotency_key="check")
    assert replay.lease_id == lease.lease_id
    await lease.confirm_proxy(reason="accepted")
    await lease.confirm_proxy(reason="repeat")
    await replay.release()
    await lease.release()
    assert await space.proxies.unbind(identity, actor="test", reason="terminal")
    assert not await space.proxies.unbind(identity, actor="test", reason="repeat")
    events = await _events(space)
    assert [event.type for event in events] == [
        "proxy.reservation.created",
        "proxy.reservation.confirmed",
        "proxy.allocation.released",
    ]
    assert all(event.data["allocation_id"] == str(lease.proxy_allocation_id) for event in events)
    assert all(event.lease_id == lease.lease_id for event in events[:2])
    assert events[-1].lease_id is None  # Команда отвязки выполняется после окончания use.
    assert all(event.identity_id == identity and event.proxy_id == lease.proxy_id for event in events)
    allowed = documented()
    assert all(event.data.keys() <= allowed[event.type] for event in events)
    dumped = repr(events)
    assert "u:pw" not in dumped and str(lease._row.token) not in dumped


async def test_cancel_live_reservation_keeps_draining_until_use_ends(space):
    _, _, (lease,) = await _setup(space)
    await space.proxies.unbind(lease.identity_id, actor="test", reason="cancel")
    assert [event.type for event in await _events(space)] == [
        "proxy.reservation.created",
        "proxy.reservation.released",
        "proxy.allocation.draining",
    ]
    await lease.release()
    await lease.release()
    assert Counter(event.type for event in await _events(space)) == {
        "proxy.reservation.created": 1,
        "proxy.reservation.released": 1,
        "proxy.allocation.draining": 1,
        "proxy.allocation.released": 1,
    }
    assert not (await space.admin.event_page(filters=EventFilters(type="proxy.binding.changed"))).items


async def test_savepoint_rollback_discards_deferred_transition_before_retry(space):
    _, _, (lease,) = await _setup(space)
    async with space._tx() as tx:
        row = await tx.proxy_allocation(lease.proxy_allocation_id)
        with pytest.raises(RuntimeError, match="savepoint rollback"):
            async with tx.savepoint():
                await confirm(tx, lease.fence, row.id, policy_fingerprint=row.policy_fingerprint)
                raise RuntimeError("savepoint rollback")
        assert (await tx.proxy_allocation(row.id)).state == "reserved"
        await confirm(tx, lease.fence, row.id, policy_fingerprint=row.policy_fingerprint)
    assert [event.type for event in await _events(space)] == [
        "proxy.reservation.created",
        "proxy.reservation.confirmed",
    ]
    await lease.release()


async def test_outer_rollback_discards_queue_and_ledger(space):
    _, _, (lease,) = await _setup(space)
    with pytest.raises(RuntimeError, match="outer rollback"):
        async with space._tx() as tx:
            row = await tx.proxy_allocation(lease.proxy_allocation_id)
            await confirm(tx, lease.fence, row.id, policy_fingerprint=row.policy_fingerprint)
            raise RuntimeError("outer rollback")
    assert [event.type for event in await _events(space)] == ["proxy.reservation.created"]
    await lease.release()


async def test_expiry_metrics_use_committed_events_and_gauges(space, hub_under_test, backend_sessionmaker):
    start = (await hub_under_test._store.current_time()).replace(second=0, microsecond=0)
    _, _, leases = await _setup(space)
    hook = RecordingMetrics()
    hub_under_test.metrics = hook
    await _expire(hub_under_test, leases, backend_sessionmaker)
    await hub_under_test._metrics_reporter.collect()
    gauges = {(event.name, event.labels.get("state")): event.value for event in hook.events}
    assert gauges[("identity_hub_proxy_slots", "reserved")] == 1
    assert gauges[("identity_hub_proxy_slots", "occupied")] == 1
    assert gauges[("identity_hub_proxy_cleanup_pending", None)] == 1
    assert await space.maintenance.cleanup_proxy_allocations() == 1
    events = await _events(space)
    released = events[-1]
    assert released.type == "proxy.allocation.released" and released.data["expired"] is True
    assert released.data["cleanup_delay_seconds"] >= 0
    assert sum(event.data["expired"] for event in events) == 2  # reservation + physical slot
    await hub_under_test.dispatcher.drain()
    await hub_under_test._metrics_reporter.collect()
    last = [event for event in hook.events if event.name == "identity_hub_proxy_slots"]
    assert last[-1].labels["state"] == "occupied" and last[-1].value == 0
    for metric in (
        "proxy.expirations",
        "proxy.reservation_seconds.count",
        "proxy.cleanup_delay_seconds.count",
    ):
        series = await space.analytics.series(
            metric,
            start=start,
            end=start + timedelta(minutes=5),
            bucket_size=BucketSize.MINUTE,
            dimensions={"group": "g"},
        )
        assert sum(group.total for group in series.groups) == 1
    assert all(
        not ({"identity_id", "lease_id", "proxy_id", "allocation_id", "binding_id"} & event.labels.keys())
        for event in hook.events
    )


async def test_proxy_refusal_metrics_distinguish_capacity_and_changed_policy(space, hub_under_test):
    _, pool, (lease,) = await _setup(space)
    hook = RecordingMetrics()
    hub_under_test.metrics = hook
    identity = (
        await register(
            space,
            service="fake_tg",
            external_ref="metrics-other",
            lifecycle="ready",
            session={"auth_key": "k"},
        )
    ).identity_id
    with pytest.raises(LeaseUnavailable):
        await pool.acquire(among=[identity])
    await space.proxies.reconcile_bindings(
        [lease.identity_id],
        service="fake_tg",
        actor="ops",
        reason="new rules",
        proxy_policy=ProxyPolicy(
            group="g", binding=ProxyBindingPolicy(bind_when=RegionIn("standing", states={"limited"}))
        ),
    )
    with pytest.raises(ProxyAssignmentConflict, match="policy_changed"):
        await lease.confirm_proxy(reason="old result")
    failures = [event for event in hook.events if event.name == "identity_hub_failures_total"]
    assert {(event.labels["scope"], event.labels["reason"]) for event in failures} == {
        ("proxy_acquire", "no_proxy"),
        ("proxy_confirm", "policy_changed"),
    }
    await lease.release()
