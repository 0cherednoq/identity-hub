from __future__ import annotations

import pytest

from identity_hub.clock import FakeClock
from identity_hub.crypto import StaticSecretsProvider
from identity_hub.errors import LeaseUnavailable
from identity_hub.hub import IdentityHub, SpaceHandle
from identity_hub.identities import RegisterResult
from identity_hub.metrics import MetricsHook, RecordingMetrics, record_metric
from identity_hub.testing import MemoryStore
from tests.fixtures.api import register
from tests.fixtures.services import FAKE_EXCHANGE


def _hub(metrics: MetricsHook | None = None) -> IdentityHub:
    clock = FakeClock()
    return IdentityHub(
        secrets=StaticSecretsProvider({"test": b"0123456789abcdef0123456789abcdef"}, "test"),
        services=[FAKE_EXCHANGE],
        store=MemoryStore(clock),
        clock=clock,
        metrics=metrics,
        strict_events=True,
    )


async def _identity(
    hub: IdentityHub, ref: str = "metrics@example.test"
) -> tuple[SpaceHandle, RegisterResult]:
    space = hub.space("tier-metrics")
    registered = await register(
        space,
        service=FAKE_EXCHANGE,
        external_ref=ref,
        credentials={"email": ref, "password": "test"},
        session={"cookies": {"sid": "test"}},
        lifecycle="ready",
    )
    return space, registered


async def test_tier_events_are_atomic_and_not_duplicated_by_reads() -> None:
    hub = _hub()
    space, registered = await _identity(hub)
    lease = await space.pools.define("tiers", service=FAKE_EXCHANGE).acquire(among=[registered.identity_id])

    email = lease.verification("email")
    await email.start()
    await email.submit()
    await space.verifications.status(registered.identity_id, "email")
    phone = lease.verification("phone")
    await phone.start()
    await phone.submit()
    kyc = lease.verification("kyc_id")
    await kyc.start()
    await kyc.submit()
    await kyc.approve()
    await email.reconcile("missing")
    await email.reconcile("missing")
    await lease.release()

    store = hub.store
    assert isinstance(store, MemoryStore)
    tiers = [
        (event.type, event.data["tier"], event.data["factor"])
        for event, _, _ in store._state.events
        if event.type.startswith("identity.tier.")
    ]
    assert tiers == [
        ("identity.tier.reached", "basic", "email"),
        ("identity.tier.reached", "full", "kyc_id"),
        ("identity.tier.lost", "basic", "email"),
        ("identity.tier.lost", "full", "email"),
    ]


async def test_lease_metrics_cover_acquisition_wait_empty_and_bound_labels() -> None:
    metrics = RecordingMetrics()
    hub = _hub(metrics)
    space, registered = await _identity(hub)
    pool = space.pools.define("metrics", service=FAKE_EXCHANGE)
    lease = await pool.acquire(among=[registered.identity_id], wait=0.1)
    await lease.release()
    with pytest.raises(LeaseUnavailable):
        await pool.acquire(among=[], wait=0)

    accepted_count = len(metrics.events)
    record_metric(metrics, "counter", "identity_hub_lease_empty_total", identity_id="secret")
    assert len(metrics.events) == accepted_count
    names = [metric.name for metric in metrics.events]
    assert "identity_hub_lease_acquisition_seconds" in names
    assert "identity_hub_lease_wait_seconds" in names
    assert "identity_hub_lease_empty_total" in names
    assert all(
        not ({"identity_id", "lease_id", "event_id", "run_id", "external_ref"} & event.labels.keys())
        for event in metrics.events
    )


async def test_broken_metrics_backend_never_breaks_domain_operations() -> None:
    class BrokenMetrics:
        def counter(self, *_: object, **__: str) -> None:
            raise RuntimeError("metrics unavailable")

        def gauge(self, *_: object, **__: str) -> None:
            raise RuntimeError("metrics unavailable")

        def histogram(self, *_: object, **__: str) -> None:
            raise RuntimeError("metrics unavailable")

    hub = _hub(BrokenMetrics())
    space, registered = await _identity(hub, "broken@example.test")
    pool = space.pools.define("broken", service=FAKE_EXCHANGE)
    lease = await pool.acquire(among=[registered.identity_id])
    async with lease.operation("view_balance"):
        pass
    await lease.release()
    await hub.metrics_reporter.collect()
