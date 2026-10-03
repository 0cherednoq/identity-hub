"""A2.7: scoped event journal filters, keyset and defensive redaction."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub.errors import NotFound
from identity_hub.hub import IdentityHub, SpaceHandle
from identity_hub.postgres.models import EventRecord
from identity_hub.read.events import EventFilters

pytestmark = pytest.mark.pg


async def test_event_journal_catalog_filters_keyset_redaction_and_scope(
    hub: IdentityHub,
    space: SpaceHandle,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    ref = await space.ref()
    occurred_at = datetime(2026, 9, 27, 10, tzinfo=UTC)
    external_ids = [
        UUID("00000000-0000-0000-0000-000000000011"),
        UUID("00000000-0000-0000-0000-000000000012"),
    ]
    journal_id = UUID("00000000-0000-0000-0000-000000000013")
    identity_id = UUID("00000000-0000-0000-0000-000000000042")
    proxy_id = UUID("00000000-0000-0000-0000-000000000071")
    lease_id = UUID("00000000-0000-0000-0000-000000000081")
    async with sessionmaker.begin() as session:
        session.add_all(
            [
                EventRecord(
                    event_id=external_ids[0],
                    type="identity.registered",
                    severity="info",
                    space_id=ref.id,
                    service="fake_tg",
                    identity_id=identity_id,
                    actor="operator@example.test",
                    data={"status": "ready", "password": "must-not-leak"},
                    external=True,
                    occurred_at=occurred_at,
                ),
                EventRecord(
                    event_id=external_ids[1],
                    type="identity.login.failed",
                    severity="warning",
                    space_id=ref.id,
                    service="fake_tg",
                    identity_id=identity_id,
                    proxy_id=proxy_id,
                    actor="worker-17",
                    data={"code": "bad_session"},
                    external=True,
                    occurred_at=occurred_at,
                ),
                EventRecord(
                    event_id=journal_id,
                    type="lease.acquired",
                    severity="info",
                    space_id=ref.id,
                    service="fake_tg",
                    identity_id=identity_id,
                    proxy_id=proxy_id,
                    lease_id=lease_id,
                    actor="worker-17",
                    data={"pool": "workers"},
                    external=True,
                    occurred_at=occurred_at,
                ),
            ]
        )

    first = await space.admin.event_page(limit=1)
    second = await space.admin.event_page(limit=1, before=first.next_before)
    assert first.next_before is not None
    assert {first.items[0].event_id, second.items[0].event_id} == set(external_ids)
    assert first.items[0].event_id != second.items[0].event_id
    assert journal_id not in {first.items[0].event_id, second.items[0].event_id}

    journal = await space.admin.event_page(
        filters=EventFilters(
            group="journal",
            include_journal=True,
            actor="worker",
            identity_id=identity_id,
            proxy_id=proxy_id,
            lease_id=lease_id,
            occurred_from=occurred_at - timedelta(minutes=1),
            occurred_to=occurred_at + timedelta(minutes=1),
        )
    )
    assert [item.event_id for item in journal.items] == [journal_id]
    assert journal.items[0].group == "journal"

    warning = await space.admin.event_page(
        filters=EventFilters(type="identity.login.failed", severity="warning", service="fake_tg")
    )
    assert [item.event_id for item in warning.items] == [external_ids[1]]
    redacted = await space.admin.event_detail(external_ids[0])
    assert redacted.data["password"] == "[redacted]"
    assert "must-not-leak" not in repr(redacted)

    other = hub.space("event-read-other", "Event read other", create=True)
    await other.ref()
    with pytest.raises(NotFound):
        await other.admin.event_detail(external_ids[0])
