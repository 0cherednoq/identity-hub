"""A2.1: safe overview aggregates and a direct SQL oracle."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub.hub import IdentityHub, SpaceHandle
from identity_hub.postgres.models import IdentityRecord, LeaseRecord
from tests.fixtures.api import register

pytestmark = pytest.mark.pg


async def test_overview_matches_sql_oracle_and_keeps_authorization_before_aggregation(
    hub: IdentityHub,
    space: SpaceHandle,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await hub.space("empty", "Empty").ref()
    await register(hub.space("hidden", "Hidden"), service="fake_tg", external_ref="hidden")
    registered = await register(
        space,
        service="fake_exchange",
        external_ref="one@example.test",
        session={"cookies": {"sid": "safe-demo"}},
        lifecycle="ready",
    )
    async with space._tx(own=True) as tx:
        deadline = await tx.now() + timedelta(hours=6)
    await space.verifications.requirements.add(registered.identity_id, "identity_document", deadline=deadline)
    lease = await space.pools.define("workers", service="fake_exchange", capacity_low=1).acquire()
    email = lease.verification("email")
    await email.start()
    await email.submit()

    overview = await space.admin.overview()
    service = overview.services[0]
    async with sessionmaker() as session:
        oracle_total = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(IdentityRecord)
                    .where(IdentityRecord.space_id == overview.space.id)
                )
            ).scalar_one()
        )
        oracle_eligible = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(IdentityRecord)
                    .where(
                        IdentityRecord.space_id == overview.space.id,
                        IdentityRecord.lifecycle == "ready",
                        IdentityRecord.standing.in_(["good", "limited"]),
                        IdentityRecord.auth_state == "authenticated",
                        IdentityRecord.availability == "available",
                    )
                )
            ).scalar_one()
        )
        oracle_leased = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(LeaseRecord)
                    .where(
                        LeaseRecord.space_id == overview.space.id,
                        LeaseRecord.expires_at > func.now(),
                    )
                )
            ).scalar_one()
        )

    assert (overview.total, overview.eligible, overview.leased) == (
        oracle_total,
        oracle_eligible,
        oracle_leased,
    )
    assert service.free_eligible == 0
    assert {factor.factor: factor.states for factor in service.factors}["email"]["verified"] == 1
    assert {tier.tier: tier.count for tier in service.tiers} == {"basic": 1, "full": 0}
    assert [(item.code, item.count, item.next_deadline) for item in service.requirements] == [
        ("identity_document", 1, deadline)
    ]

    visible = await hub.spaces_matrix({"empty", "tennet"})
    assert [item.space.slug for item in visible.spaces] == ["empty", "tennet"]
    assert sum(item.total for item in visible.spaces) == 1
    assert "hidden" not in {item.space.slug for item in visible.spaces}

    capacity = await space.admin.pool_capacity(
        space.pools.define("workers", service="fake_exchange", capacity_low=1)
    )
    assert (capacity.eligible, capacity.leased, capacity.free, capacity.state) == (1, 1, 0, "empty")
    await lease.release()
