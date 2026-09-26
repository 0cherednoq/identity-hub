"""P2.3: регионы в БД (ТЗ FR-9.1–9.4, 9.6, 9.8, G-4.1, G-6.1)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub.domain.regions import Command, Observation, Outcome
from identity_hub.enums import AuthState, Availability, ChangeSource, Lifecycle, Standing
from identity_hub.errors import NotFound, TransitionNotAllowed, ValidationFailed
from identity_hub.hub import IdentityHub, SpaceHandle
from identity_hub.postgres.models import EventRecord, IdentityRecord, RegionStateRecord
from identity_hub.state import Override
from tests.pg.helpers import make_identity

pytestmark = pytest.mark.pg


async def _identity_row(sm: async_sessionmaker[AsyncSession], identity_id: UUID) -> Any:
    async with sm() as s:
        return (await s.execute(select(IdentityRecord).where(IdentityRecord.id == identity_id))).scalar_one()


async def _region_row(
    sm: async_sessionmaker[AsyncSession], identity_id: UUID, region: str, key: str = ""
) -> Any:
    async with sm() as s:
        return (
            await s.execute(
                select(RegionStateRecord).where(
                    RegionStateRecord.identity_id == identity_id,
                    RegionStateRecord.region == region,
                    RegionStateRecord.key == key,
                )
            )
        ).scalar_one_or_none()


async def _event_types(sm: async_sessionmaker[AsyncSession]) -> list[str]:
    async with sm() as s:
        return list((await s.execute(select(EventRecord.type).order_by(EventRecord.id))).scalars())


async def _now(space: SpaceHandle) -> Any:
    async with space._tx() as tx:
        return await tx.now()


async def test_lifecycle_command_and_event(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    identity_id = await make_identity(space, lifecycle="provisioning")
    decision = await space.set_lifecycle(identity_id, Lifecycle.READY, actor="admin")
    assert decision.transitioned
    assert (await _identity_row(sessionmaker, identity_id)).lifecycle == "ready"
    with pytest.raises(TransitionNotAllowed):
        await space.set_lifecycle(identity_id, Lifecycle.PROVISIONING, actor="admin")
    await space.retire(identity_id, actor="admin", reason="продан")
    with pytest.raises(TransitionNotAllowed, match="причина"):
        await space.set_lifecycle(identity_id, Lifecycle.READY, actor="admin")
    assert await _event_types(sessionmaker) == ["identity.lifecycle.changed", "identity.lifecycle.changed"]


async def test_unknown_identity_and_foreign_space(hub: IdentityHub, space: SpaceHandle) -> None:
    identity_id = await make_identity(space)
    with pytest.raises(NotFound):
        await hub.space("other").set_lifecycle(identity_id, Lifecycle.RETIRED, actor="a")


async def test_observation_writes_region_denormalizes_and_emits(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    identity_id = await make_identity(space)
    now = await _now(space)
    obs = Observation(
        Standing.LIMITED,
        observed_at=now,
        source=ChangeSource.PROBE,
        raw_status="SPAM",
        code="spam_restricted",
    )
    decision = await space._observe(identity_id, "standing", obs)
    assert decision.outcome is Outcome.APPLIED

    row = await _identity_row(sessionmaker, identity_id)
    assert (row.standing, row.standing_code) == ("limited", "spam_restricted")
    region = await _region_row(sessionmaker, identity_id, "standing")
    assert region.state == "limited"
    assert region.change_source == "probe"
    assert region.raw_status == "SPAM"
    assert region.synced_at is not None
    assert region.version == 2

    async with sessionmaker() as s:
        event = (await s.execute(select(EventRecord))).scalar_one()
    assert event.type == "identity.standing.changed"
    assert event.severity == "warning"
    assert event.data["to"] == "limited"
    assert event.service == "fake_tg"


async def test_stale_observation_dropped(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    identity_id = await make_identity(space)
    now = await _now(space)
    await space._observe(identity_id, "standing", Observation(Standing.LIMITED, observed_at=now))
    stale = await space._observe(
        identity_id, "standing", Observation(Standing.GOOD, observed_at=now - timedelta(1))
    )
    assert stale.outcome is Outcome.STALE
    assert (await _identity_row(sessionmaker, identity_id)).standing == "limited"


async def test_override_pin_and_drift(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    identity_id = await make_identity(space, standing="banned")
    await space.override(
        identity_id,
        Override(
            "standing", "", Standing.GOOD, reason="бан ложный", actor="evgeny", pin_for=timedelta(hours=6)
        ),
    )
    region = await _region_row(sessionmaker, identity_id, "standing")
    assert (region.state, region.change_source, region.change_reason, region.changed_by) == (
        "good",
        "override",
        "бан ложный",
        "evgeny",
    )
    assert region.pinned_until is not None

    now = await _now(space)
    pinned = await space._observe(
        identity_id, "standing", Observation(Standing.BANNED, now, raw_status="BAN")
    )
    assert pinned.outcome is Outcome.PINNED
    row = await _identity_row(sessionmaker, identity_id)
    assert row.standing == "good"
    assert (await _region_row(sessionmaker, identity_id, "standing")).raw_status == "BAN"
    assert await _event_types(sessionmaker) == [
        "identity.standing.changed",
        "identity.state.overridden",
        "identity.state.drift",
    ]


async def test_override_requires_reason(space: SpaceHandle) -> None:
    identity_id = await make_identity(space)
    with pytest.raises(ValidationFailed):
        await space.override(identity_id, Override("standing", "", Standing.BANNED, reason="", actor="a"))
    with pytest.raises(ValidationFailed):
        await space.override(
            identity_id, Override("lifecycle", "", "ready", reason="r", actor="a", pin_for=timedelta(1))
        )


async def test_confirm_leaving_frozen(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    identity_id = await make_identity(space)
    now = await _now(space)
    await space._observe(identity_id, "standing", Observation(Standing.FROZEN, now))
    first = await space._observe(
        identity_id, "standing", Observation(Standing.GOOD, now + timedelta(seconds=1))
    )
    assert first.outcome is Outcome.AWAITING_CONFIRMATION
    assert (await _identity_row(sessionmaker, identity_id)).standing == "frozen"
    second = await space._observe(
        identity_id, "standing", Observation(Standing.GOOD, now + timedelta(seconds=2))
    )
    assert second.outcome is Outcome.APPLIED
    assert (await _identity_row(sessionmaker, identity_id)).standing == "good"


async def test_auth_default_slot_denormalized_other_slots_not(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    identity_id = await make_identity(space)
    now = await _now(space)
    await space._observe(identity_id, "auth", Observation(AuthState.SESSION_INVALID, now), key="backup")
    assert (await _identity_row(sessionmaker, identity_id)).auth_state == "authenticated"
    await space._observe(identity_id, "auth", Observation(AuthState.SESSION_INVALID, now), key="default")
    row = await _identity_row(sessionmaker, identity_id)
    assert row.auth_state == "session_invalid"
    assert row.standing == "good"  # session_invalid — не бан


async def test_availability_cooldown_only_extends_and_expires(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    identity_id = await make_identity(space)
    now = await _now(space)
    long_until = now + timedelta(minutes=10)
    await space._command(
        identity_id, "availability", Command(Availability.COOLING, actor="w", until=long_until)
    )
    await space._command(
        identity_id,
        "availability",
        Command(Availability.COOLING, actor="w", until=now + timedelta(minutes=1)),
    )
    row = await _identity_row(sessionmaker, identity_id)
    assert (row.availability, row.availability_until) == ("cooling", long_until)
    not_due = await space._expire(identity_id, "availability")
    assert not_due.outcome is Outcome.NOT_DUE


async def test_override_inside_user_transaction(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    identity_id = await make_identity(space, standing="banned")
    async with sessionmaker() as user:
        await user.begin()
        await space.using(user).override(identity_id, Override("standing", "", "good", reason="r", actor="a"))
        await user.rollback()
    assert (await _identity_row(sessionmaker, identity_id)).standing == "banned"
    assert await _event_types(sessionmaker) == []


async def test_concurrent_observations_serialize(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    identity_id = await make_identity(space)
    now = await _now(space)
    await space._observe(identity_id, "standing", Observation(Standing.GOOD, now))  # строка региона создана
    results = await asyncio.gather(
        *(
            space._observe(
                identity_id,
                "standing",
                Observation(
                    Standing.LIMITED if i % 2 else Standing.GOOD, now + timedelta(seconds=i + 1), code=str(i)
                ),
            )
            for i in range(8)
        )
    )
    assert all(r.outcome is not None for r in results)
    region = await _region_row(sessionmaker, identity_id, "standing")
    row = await _identity_row(sessionmaker, identity_id)
    assert row.standing == region.state  # денормализация согласована
    assert region.version >= 2
