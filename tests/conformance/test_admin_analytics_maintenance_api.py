"""IMP-23 (I8.8a): `space.admin`, `space.analytics`, `space.maintenance`."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from identity_hub import IdentityCreate, IdentityHub, Lifecycle, SpaceHandle

pytestmark = [pytest.mark.conformance]


async def test_admin_read_models(space: SpaceHandle) -> None:
    identity = await space.identities.create(
        IdentityCreate(
            service="fake_tg", external_ref="+10", lifecycle=Lifecycle.READY, tags=frozenset({"a"})
        ),
        actor="test",
    )

    overview = await space.admin.overview()
    page = await space.admin.identity_page("tag:a")
    snapshot = await space.admin.selection_snapshot("tag:a")
    summary = await space.admin.pool_summary("fake_tg")

    assert overview is not None
    assert [item.id for item in page.items] == [identity.id]
    assert [entry.id for entry in snapshot.entries] == [identity.id] and not snapshot.truncated
    assert summary["fake_tg"]["lifecycle"]["ready"] == 1
    assert (await space.admin.link_summary([identity.id]))[identity.id].outgoing == 0
    assert (await space.admin.event_page()).items
    assert await space.admin.lease_history(identity.id) == []


async def test_admin_audit_records_event(space: SpaceHandle) -> None:
    await space.admin.record_bulk(
        "retire", actor="admin", reason="чистка", snapshot_id=uuid4(), succeeded=1, failed=0, attempt=1
    )
    page = await space.admin.event_page()
    assert any(item.type == "admin.bulk.executed" for item in page.items)


async def test_analytics_reports(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    now = hub_under_test.clock.now()
    funnel = await space.analytics.funnel(start=now - timedelta(days=1), end=now)
    utilization = await space.analytics.utilization(start=now - timedelta(days=1), end=now)
    assert funnel is not None and utilization is not None


async def test_maintenance_operations(space: SpaceHandle) -> None:
    assert await space.maintenance.expire_challenges() == 0
    assert await space.maintenance.expire_verifications() == 0
    assert await space.maintenance.reindex_search("fake_tg") == 0
