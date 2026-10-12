"""P3.1: пул и выборка кандидатов (ТЗ FR-6.1, G-1.1, G-1.2, §3.9, §8.5)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub._ids import new_id
from identity_hub.errors import ValidationFailed
from identity_hub.hub import SpaceHandle
from identity_hub.postgres.models import IdentityRecord, LeaseRecord
from tests.pg.helpers import make_identity

pytestmark = pytest.mark.pg


async def _set(sm: async_sessionmaker[AsyncSession], identity_id: UUID, **values: Any) -> None:
    async with sm() as s, s.begin():
        await s.execute(update(IdentityRecord).where(IdentityRecord.id == identity_id).values(**values))


def test_timing_validation(space: SpaceHandle) -> None:
    with pytest.raises(ValidationFailed, match="TTL/3"):
        space.pools.define("p", service="fake_tg", ttl=timedelta(seconds=30), heartbeat=timedelta(seconds=20))
    with pytest.raises(ValidationFailed, match="safety_margin"):
        space.pools.define(
            "p", service="fake_tg", heartbeat=timedelta(seconds=10), safety_margin=timedelta(seconds=5)
        )
    with pytest.raises(ValidationFailed, match="filterable"):
        space.pools.define("p", service="fake_tg", profile={"first_name": "x"})


@pytest.mark.parametrize(
    ("values", "picked"),
    [
        ({}, True),
        ({"lifecycle": "provisioning"}, False),
        ({"auth_state": "session_invalid"}, False),
        ({"standing": "limited"}, True),
        ({"standing": "frozen"}, False),
        ({"availability": "quarantined"}, False),
        ({"availability": "cooling", "availability_until": text("now() + interval '1 minute'")}, False),
        ({"availability": "paused", "availability_until": text("now() - interval '1 second'")}, True),
    ],
)
async def test_region_filters(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession], values: dict[str, Any], picked: bool
) -> None:
    identity_id = await make_identity(space)
    if values:
        await _set(sessionmaker, identity_id, **values)
    row = await space.pools.define("p", service="fake_tg")._pick()
    assert (row is not None and row.id == identity_id) is picked


async def test_for_operation_rules(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    limited = await make_identity(space, standing="limited")
    pool = space.pools.define("p", service="fake_tg")
    assert (await pool._pick(for_operation="read_channel")) is not None
    assert await pool._pick(for_operation="send_message") is None  # только good
    unauth = await make_identity(space, service="fake_tg", auth_state="no_session", standing="banned")
    probe = await pool._pick(for_operation="probe", exclude=[limited])
    assert probe is not None and probe.id == unauth

    ex = space.pools.define("ex", service="fake_exchange")
    partial = await make_identity(space, service="fake_exchange", verified_factors=["email", "phone"])
    assert await ex._pick(for_operation="withdraw") is None
    await _set(sessionmaker, partial, verified_factors=["email", "phone", "kyc_id"])
    assert (await ex._pick(for_operation="withdraw")) is not None


async def test_tags_labels_profile_among(space: SpaceHandle) -> None:
    a = await make_identity(space, tags=["parser", "ua"], labels={"farm": "3"}, profile={"country": "UA"})
    b = await make_identity(space, tags=["parser"], labels={"farm": "4"}, profile={"country": "PL"})
    pool = space.pools.define("parsers", service="fake_tg", tags={"parser"})
    assert (await pool._pick(tags=["ua"])).id == a  # type: ignore[union-attr]
    assert (await pool._pick(labels={"farm": "4"})).id == b  # type: ignore[union-attr]
    assert (await pool._pick(profile={"country": "UA"})).id == a  # type: ignore[union-attr]
    assert (await pool._pick(among=[b])).id == b  # type: ignore[union-attr]
    domain = select(IdentityRecord.id).where(IdentityRecord.external_ref == str(a))
    assert (await pool._pick(among=domain)).id == a  # type: ignore[union-attr]
    assert await pool._pick(among=[new_id()]) is None
    assert await space.pools.define("x", service="fake_tg", tags={"nope"})._pick() is None


async def test_live_lease_excluded_and_least_recently_leased_first(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    ref = await space.ref()
    ids = [await make_identity(space) for _ in range(4)]
    old, recent, busy, fresh = ids
    async with sessionmaker() as s, s.begin():
        # время последнего захвата хранится в строке аккаунта (D-209); живая аренда — только у busy
        for identity_id, acquired in (
            (old, "now() - interval '2 hours'"),
            (recent, "now() - interval '1 minute'"),
            (busy, "now()"),
        ):
            await s.execute(
                update(IdentityRecord)
                .where(IdentityRecord.id == identity_id)
                .values(last_leased_at=text(acquired), pick_order_at=text(acquired))
            )
        await s.execute(
            LeaseRecord.__table__.insert().values(
                identity_id=busy,
                space_id=ref.id,
                lease_id=new_id(),
                fencing=1,
                pool="p",
                holder="h",
                acquired_at=text("now()"),
                expires_at=text("now() + interval '1 minute'"),
            )
        )
    pool = space.pools.define("p", service="fake_tg")
    order = []
    exclude: list[UUID] = []
    while (row := await pool._pick(exclude=exclude)) is not None:
        order.append(row.id)
        exclude.append(row.id)
    assert busy not in order
    assert order.index(fresh) < order.index(old) < order.index(recent)


async def test_skip_locked(space: SpaceHandle) -> None:
    a = await make_identity(space)
    b = await make_identity(space)
    pool = space.pools.define("p", service="fake_tg")
    query = pool.candidate_query()
    first_picked = asyncio.Event()
    release = asyncio.Event()
    seen: list[UUID] = []

    async def holder() -> None:
        async with space._tx(own=True) as tx:
            row = await tx.pick_candidate(query)
            assert row is not None
            seen.append(row.id)
            first_picked.set()
            await release.wait()

    task = asyncio.create_task(holder())
    await first_picked.wait()
    second = await pool._pick()
    release.set()
    await task
    assert second is not None and second.id != seen[0]
    assert {seen[0], second.id} == {a, b}


async def test_pick_uses_partial_index(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await make_identity(space)
    ref = await space.ref()
    async with sessionmaker() as s, s.begin():
        await s.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join(
            (
                await s.execute(
                    text(
                        "EXPLAIN SELECT id FROM ih_identities "
                        "WHERE space_id = :space AND service = 'fake_tg' "
                        "AND lifecycle = 'ready' AND auth_state = 'authenticated' "
                        "AND standing IN ('good', 'limited') AND availability = 'available'"
                    ),
                    {"space": ref.id},
                )
            ).scalars()
        )
    assert "ix_ih_identities_pick" in plan
