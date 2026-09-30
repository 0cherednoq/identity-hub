"""P9.1: периодическое обслуживание и retention (ТЗ §8.9, FR-6.8)."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
import respx
from pydantic import SecretStr
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub.domain.regions import Command
from identity_hub.enums import Availability
from identity_hub.events import EventDraft
from identity_hub.hub import IdentityHub, SpaceHandle
from identity_hub.postgres.models import (
    EventRecord,
    IdentityRecord,
    ImportRunRecord,
    LeaseHistoryRecord,
    LeaseLogRecord,
    LeaseRecord,
    OperationKeyRecord,
    ProviderOperationRecord,
    QuotaUsageRecord,
    WebhookDeliveryRecord,
)
from tests.pg.helpers import make_identity

pytestmark = pytest.mark.pg


async def test_normalizes_timers_and_closes_expired_lease_at_expires_at(
    hub: IdentityHub,
    space: SpaceHandle,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    identity_id = await make_identity(space)
    lease = await space.pool("maintenance", service="fake_tg").acquire()
    async with space._tx() as tx:
        now = await tx.now()
    await space._command(
        identity_id,
        "availability",
        Command(Availability.COOLING, actor="test", until=now + timedelta(minutes=5)),
    )
    async with sessionmaker() as session, session.begin():
        await session.execute(
            update(IdentityRecord).values(availability_until=text("now() - interval '1 second'"))
        )
        await session.execute(update(LeaseRecord).values(expires_at=text("now() - interval '5 seconds'")))
        expired_at = (await session.execute(select(LeaseRecord.expires_at))).scalar_one()

    report = await hub.maintenance.run_all()

    assert report.counts["region_timers"] == 1
    assert report.counts["leases"] == 1
    async with sessionmaker() as session:
        identity = (await session.execute(select(IdentityRecord))).scalar_one()
        history = (
            await session.execute(
                select(LeaseHistoryRecord).where(LeaseHistoryRecord.lease_id == lease.lease_id)
            )
        ).scalar_one()
        types = list((await session.execute(select(EventRecord.type))).scalars())
    assert identity.availability == "available" and identity.availability_until is None
    assert history.end_reason == "expired" and history.released_at == expired_at
    assert types.count("lease.expired") == 1


@respx.mock
async def test_cleanup_removes_expired_windows_and_old_retained_rows(
    hub: IdentityHub,
    space: SpaceHandle,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    identity_id = await make_identity(space)
    lease = await space.pool("cleanup", service="fake_tg").acquire()
    await lease.release()
    endpoint = await space.webhooks.create(
        url="https://hooks.example.test/maintenance",
        secret=SecretStr("maintenance-secret"),
        event_types=["pool.capacity.low"],
    )
    respx.post(endpoint.url).mock(return_value=httpx.Response(204))
    async with space._tx() as tx:
        await space._emit(tx, EventDraft("pool.capacity.low", actor="test"))
    await hub.dispatcher.drain()

    ref = await space.ref()
    async with sessionmaker() as session, session.begin():
        old = text("now() - interval '400 days'")
        await session.execute(
            insert(QuotaUsageRecord).values(
                identity_id=identity_id,
                kind="old",
                window_seconds=60,
                window_start=old,
                space_id=ref.id,
                used=1,
            )
        )
        await session.execute(
            insert(OperationKeyRecord).values(
                key="old-key",
                identity_id=identity_id,
                space_id=ref.id,
                kind="old",
                outcome="ok",
                created_at=old,
            )
        )
        await session.execute(
            insert(LeaseLogRecord).values(
                lease_id=lease.lease_id,
                identity_id=identity_id,
                space_id=ref.id,
                ts=old,
                level=20,
                message="old",
                fields={},
            )
        )
        await session.execute(update(LeaseHistoryRecord).values(released_at=old))
        await session.execute(update(EventRecord).values(occurred_at=old))
        await session.execute(update(WebhookDeliveryRecord).values(created_at=old))
        await session.execute(
            insert(ImportRunRecord).values(
                space_id=ref.id,
                service="fake_tg",
                actor="admin:test",
                source="old.jsonl",
                state="succeeded",
                finished_at=old,
            )
        )
        await session.execute(
            insert(ImportRunRecord).values(
                space_id=ref.id,
                service="fake_tg",
                actor="admin:test",
                source="active.jsonl",
                state="running",
            )
        )
        await session.execute(
            insert(ProviderOperationRecord).values(
                space_id=ref.id,
                identity_id=identity_id,
                kind="probe:auth",
                state="succeeded",
                finished_at=old,
            )
        )
        await session.execute(
            insert(ProviderOperationRecord).values(
                space_id=ref.id,
                identity_id=identity_id,
                kind="login",
                state="running",
            )
        )

    report = await hub.maintenance.run_all()

    assert report.counts["quota_windows"] == 1
    assert report.counts["operation_keys"] == 1
    assert report.counts["lease_logs"] == 1
    assert report.counts["lease_history"] == 1
    assert report.counts["webhook_deliveries"] == 1
    assert report.counts["events"] >= 1
    assert report.counts["import_runs"] == 1
    assert report.counts["provider_operations"] == 1
    async with sessionmaker() as session:
        for model in (
            QuotaUsageRecord,
            OperationKeyRecord,
            LeaseLogRecord,
            LeaseHistoryRecord,
            WebhookDeliveryRecord,
            EventRecord,
        ):
            assert (await session.execute(select(func.count()).select_from(model))).scalar_one() == 0
        assert (await session.execute(select(func.count()).select_from(ImportRunRecord))).scalar_one() == 1
        assert (
            await session.execute(select(func.count()).select_from(ProviderOperationRecord))
        ).scalar_one() == 1
