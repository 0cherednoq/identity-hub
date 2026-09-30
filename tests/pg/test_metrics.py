"""P9.3: точки инструментирования lease/operation/pool/dispatcher/delivery."""

from __future__ import annotations

import pytest

from identity_hub.events import EventDraft
from identity_hub.hub import IdentityHub, SpaceHandle
from identity_hub.metrics import RecordingMetrics
from tests.pg.helpers import make_identity

pytestmark = pytest.mark.pg


async def test_metrics_hook_called_at_required_points(hub: IdentityHub, space: SpaceHandle) -> None:
    metrics = RecordingMetrics()
    hub.metrics = metrics
    await make_identity(space)
    pool = space.pool("metrics", service="fake_tg", capacity_low=1)
    assert await pool.check_capacity() == 1

    async with pool.lease(wait=0.1) as lease:
        async with lease.operation("read_channel"):
            pass
        with pytest.raises(RuntimeError, match="boom"):
            async with lease.operation("read_channel"):
                raise RuntimeError("boom")

    async with space._tx() as tx:
        await space._emit(tx, EventDraft("pool.capacity.low", actor="metrics"))
    await hub.dispatcher.drain()
    await hub.metrics_reporter.collect()

    names = {event.name for event in metrics.events}
    assert names >= {
        "identity_hub_lease_duration_seconds",
        "identity_hub_lease_acquisition_seconds",
        "identity_hub_lease_wait_seconds",
        "identity_hub_operation_duration_seconds",
        "identity_hub_failures_total",
        "identity_hub_pool_available",
        "identity_hub_dispatcher_cursor_lag",
        "identity_hub_delivery_queue",
    }
    operation_statuses = {
        event.labels["status"]
        for event in metrics.events
        if event.name == "identity_hub_operation_duration_seconds"
    }
    assert operation_statuses == {"succeeded", "failed"}
    queues = {
        event.labels["queue"] for event in metrics.events if event.name == "identity_hub_delivery_queue"
    }
    assert queues == {"webhooks", "notifications"}
    forbidden = {"identity_id", "lease_id", "event_id", "run_id", "external_ref"}
    assert all(not (forbidden & event.labels.keys()) for event in metrics.events)
