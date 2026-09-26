"""P5.5: пробы, resync, reconcile (ТЗ FR-9.3, 9.6–9.8)."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub.errors import ValidationFailed
from identity_hub.hub import IdentityHub, SpaceHandle
from identity_hub.postgres.models import EventRecord, IdentityRecord, QuotaUsageRecord, RegionStateRecord
from identity_hub.protocols import ProbeContext, ProbeResult
from identity_hub.service import OperationRule, Quota
from identity_hub.state import Override
from tests.fixtures.services import FAKE_TG

pytestmark = pytest.mark.pg


class FakeProbes:
    def __init__(self) -> None:
        self.standing_result = ProbeResult(state="good", raw_status="OK")
        self.auth_result: ProbeResult | None = ProbeResult(state="authenticated")
        self.calls: list[tuple[str, UUID]] = []

    async def auth(self, ctx: ProbeContext) -> ProbeResult | None:
        self.calls.append(("auth", ctx.identity_id))
        return self.auth_result

    async def standing(self, ctx: ProbeContext) -> ProbeResult | None:
        self.calls.append(("standing", ctx.identity_id))
        assert await ctx.session() is not None
        return self.standing_result

    async def verification(self, ctx: ProbeContext, factor: str) -> ProbeResult | None:
        return None


@pytest.fixture
async def probes(hub: IdentityHub) -> FakeProbes:
    fake = FakeProbes()
    hub.register_service(
        replace(
            FAKE_TG,
            name="tg_probe",
            probes=fake,
            operations={
                **FAKE_TG.operations,
                "probe": OperationRule(
                    needs_auth=False,
                    standing=frozenset({"good", "limited", "frozen", "appealing", "banned", "deleted"}),
                    quotas=(Quota(2, timedelta(hours=1)),),
                ),
            },
        )
    )
    await hub.start()
    return fake


async def _reg(space: SpaceHandle, ref: str) -> UUID:
    r = await space.register(
        service="tg_probe", external_ref=ref, session={"auth_key": "k"}, lifecycle="ready"
    )
    return r.identity_id


async def _row(sm: async_sessionmaker[AsyncSession], identity_id: UUID) -> Any:
    async with sm() as s:
        return (await s.execute(select(IdentityRecord).where(IdentityRecord.id == identity_id))).scalar_one()


async def test_resync_applies_observations(
    probes: FakeProbes, space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    identity_id = await _reg(space, "+1")
    probes.standing_result = ProbeResult(state=None, code="spam_restricted", raw_status="SPAM")
    result = await space.resync(identity_id)
    assert result == {"auth": "unchanged", "standing": "applied"}
    row = await _row(sessionmaker, identity_id)
    assert (row.standing, row.standing_code) == ("limited", "spam_restricted")
    async with sessionmaker() as s:
        region = (
            await s.execute(select(RegionStateRecord).where(RegionStateRecord.region == "standing"))
        ).scalar_one()
        used = (await s.execute(select(QuotaUsageRecord.used))).scalar_one()
    assert (region.change_source, region.raw_status, region.synced_at is not None) == ("probe", "SPAM", True)
    assert used == 1  # проба тратит квоту вида probe


async def test_unknown_status_and_pinned_drift(
    probes: FakeProbes, space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    identity_id = await _reg(space, "+1")
    probes.standing_result = ProbeResult(state=None, raw_status="WEIRD")
    assert (await space.resync(identity_id, ["standing"]))["standing"] == "unknown_status"
    assert (await _row(sessionmaker, identity_id)).standing == "good"

    await space.override(
        identity_id,
        Override("standing", "", "good", reason="проверено", actor="admin", pin_for=timedelta(hours=1)),
    )
    probes.standing_result = ProbeResult(state="banned", raw_status="BANNED")
    assert (await space.resync(identity_id, ["standing"]))["standing"] == "pinned"
    assert (await _row(sessionmaker, identity_id)).standing == "good"
    async with sessionmaker() as s:
        types = list((await s.execute(select(EventRecord.type))).scalars())
    assert "identity.state.drift" in types


async def test_reconcile_skips_busy_and_respects_age(
    probes: FakeProbes, space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    a = await _reg(space, "+1")
    b = await _reg(space, "+2")
    busy = await space.pool("w", service="tg_probe").acquire(among=[b])
    report = await space.reconcile(["standing"], service="tg_probe")
    assert list(report.probed) == [a]
    assert report.skipped_busy == [b]
    await busy.release()
    again = await space.reconcile(["standing"], service="tg_probe", not_synced_for=timedelta(hours=1))
    assert list(again.probed) == [b]  # a сверен только что


async def test_resync_validation(probes: FakeProbes, space: SpaceHandle) -> None:
    identity_id = await _reg(space, "+1")
    with pytest.raises(ValidationFailed):
        await space.resync(identity_id, ["lifecycle"])
