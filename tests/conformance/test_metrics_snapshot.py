"""F4.4b (R2-P03): сводка фоновых процессов с фильтром пространств одинакова на обоих хранилищах."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import SecretStr

from identity_hub import IdentityHub, SpaceHandle
from identity_hub.events import EventDraft

pytestmark = pytest.mark.conformance


async def _queued_delivery(hub: IdentityHub, space: SpaceHandle) -> None:
    await space.webhooks.create(
        url=f"https://hooks.example.test/{space.slug}",
        secret=SecretStr("s"),
        event_types=["identity.updated"],
    )
    async with space._tx(own=True) as tx:
        await space._emit(tx, EventDraft("identity.updated", actor="test"))
    await hub._store.process_events("webhooks", hub._webhook_dispatcher.handle_events, limit=100)


async def test_metrics_snapshot_counts_only_allowed_spaces(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    other = hub_under_test.space(f"o-{uuid4().hex[:8]}", create=True)
    await _queued_delivery(hub_under_test, space)
    await _queued_delivery(hub_under_test, other)
    store = hub_under_test._store

    everything = await store.metrics_snapshot()
    mine = await store.metrics_snapshot(frozenset({space.slug}))
    nobody = await store.metrics_snapshot(frozenset({f"absent-{uuid4().hex[:6]}"}))

    assert everything.delivery_queue["webhooks"] >= 2
    assert mine.delivery_queue["webhooks"] == 1
    assert nobody.delivery_queue["webhooks"] == 0


async def test_background_snapshot_with_allowed_spaces(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    """Админка с ограничением по пространствам открывает страницу фоновых процессов."""
    await _queued_delivery(hub_under_test, space)

    snapshot = await hub_under_test.background_snapshot(allowed_spaces=[space.slug, "other"])

    assert snapshot.processes
