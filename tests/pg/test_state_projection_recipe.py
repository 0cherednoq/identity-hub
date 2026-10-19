"""SP4.1: executable recipe for an external revision-guarded identity projection."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID, uuid4

import pytest
from sqlalchemy import BigInteger, Column, MetaData, Table, Text, delete, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.types import Uuid

from identity_hub import IdentityHub, SpaceHandle
from identity_hub.dispatch.dispatcher import Dispatcher
from identity_hub.events import Event
from tests.fixtures.api import register

pytestmark = pytest.mark.pg


@pytest.fixture
async def projection(db: AsyncEngine) -> Table:
    table = Table(
        f"external_identity_projection_{uuid4().hex[:8]}",
        MetaData(),
        Column("identity_id", Uuid, primary_key=True),
        Column("revision", BigInteger, nullable=False),
        Column("note", Text),
    )
    async with db.begin() as connection:
        await connection.run_sync(table.metadata.create_all)
    try:
        yield table
    finally:
        async with db.begin() as connection:
            await connection.run_sync(table.metadata.drop_all)


def _upsert(table: Table, values: dict[str, object]) -> object:
    statement = insert(table).values(**values)
    return statement.on_conflict_do_update(
        index_elements=[table.c.identity_id],
        set_={"revision": statement.excluded.revision, "note": statement.excluded.note},
        where=statement.excluded.revision > table.c.revision,
    )


def _consumer(
    hub: IdentityHub,
    space: SpaceHandle,
    sessionmaker: async_sessionmaker[AsyncSession],
    projection: Table,
) -> Callable[[list[Event]], Awaitable[None]]:
    async def apply(events: list[Event]) -> None:
        ids = {event.identity_id for event in events if event.identity_id is not None}
        if not ids:
            return
        ref = await space.ref()
        async with sessionmaker() as session, session.begin():
            rows = (
                await session.execute(
                    select(
                        hub.read.identities.c.id,
                        hub.read.identities.c.revision,
                        hub.read.identities.c.note,
                    ).where(
                        hub.read.identities.c.space_id == ref.id,
                        hub.read.identities.c.id.in_(ids),
                    )
                )
            ).mappings()
            present = set()
            for row in rows:
                present.add(row["id"])
                await session.execute(
                    _upsert(
                        projection,
                        {
                            "identity_id": row["id"],
                            "revision": row["revision"],
                            "note": row["note"],
                        },
                    )
                )
            deleted = ids - present
            if deleted:
                await session.execute(delete(projection).where(projection.c.identity_id.in_(deleted)))

    return apply


async def _snapshot(
    hub: IdentityHub,
    space: SpaceHandle,
    sessionmaker: async_sessionmaker[AsyncSession],
    projection: Table,
    *,
    page_size: int = 100,
    after_page: Callable[[tuple[UUID, ...]], Awaitable[None]] | None = None,
) -> None:
    ref = await space.ref()
    after: UUID | None = None
    while True:
        statement = (
            select(
                hub.read.identities.c.id,
                hub.read.identities.c.revision,
                hub.read.identities.c.note,
            )
            .where(hub.read.identities.c.space_id == ref.id)
            .order_by(hub.read.identities.c.id)
            .limit(page_size)
        )
        if after is not None:
            statement = statement.where(hub.read.identities.c.id > after)
        async with sessionmaker() as session:
            rows = list((await session.execute(statement)).mappings())
        if not rows:
            return
        async with sessionmaker() as session, session.begin():
            for row in rows:
                await session.execute(
                    _upsert(
                        projection,
                        {
                            "identity_id": row["id"],
                            "revision": row["revision"],
                            "note": row["note"],
                        },
                    )
                )
        if after_page is not None:
            await after_page(tuple(row["id"] for row in rows))
            after_page = None
        after = rows[-1]["id"]


async def _assert_current(
    hub: IdentityHub,
    space: SpaceHandle,
    sessionmaker: async_sessionmaker[AsyncSession],
    projection: Table,
) -> None:
    ref = await space.ref()
    async with sessionmaker() as session:
        expected = set(
            (
                await session.execute(
                    select(
                        hub.read.identities.c.id,
                        hub.read.identities.c.revision,
                        hub.read.identities.c.note,
                    ).where(hub.read.identities.c.space_id == ref.id)
                )
            ).all()
        )
        actual = set((await session.execute(select(projection).order_by(projection.c.identity_id))).all())
    assert actual == expected


async def test_change_during_snapshot_is_replayed_after_snapshot(
    hub: IdentityHub,
    space: SpaceHandle,
    sessionmaker: async_sessionmaker[AsyncSession],
    projection: Table,
) -> None:
    for number in (1, 2):
        await register(space, service="fake_tg", external_ref=f"snapshot-{number}")
    handler = _consumer(hub, space, sessionmaker, projection)
    hub.events.subscribe_batch(
        "external-snapshot", handler, types={"identity.*"}, start="now", failure_policy="block"
    )
    await hub.dispatcher.run_once()  # durable cursor exists before the first snapshot page

    async def mutate(ids: tuple[UUID, ...]) -> None:
        await space.identities.set_note(ids[0], "changed-during-snapshot", actor="test")

    await _snapshot(hub, space, sessionmaker, projection, page_size=1, after_page=mutate)
    await hub.dispatcher.drain()
    await _assert_current(hub, space, sessionmaker, projection)


async def test_consumer_resumes_from_durable_cursor_after_restart(
    hub: IdentityHub,
    space: SpaceHandle,
    sessionmaker: async_sessionmaker[AsyncSession],
    projection: Table,
) -> None:
    identity = await register(space, service="fake_tg", external_ref="restart")
    handler = _consumer(hub, space, sessionmaker, projection)
    hub.events.subscribe_batch(
        "external-restart", handler, types={"identity.*"}, start="now", failure_policy="block"
    )
    await hub.dispatcher.run_once()
    await _snapshot(hub, space, sessionmaker, projection)
    await space.identities.set_note(identity.identity_id, "before-stop", actor="test")
    await hub.dispatcher.run_once()

    await space.identities.set_note(identity.identity_id, "while-stopped", actor="test")
    restarted = Dispatcher(hub)
    restarted.subscribe_batch(
        "external-restart", handler, types={"identity.*"}, start="now", failure_policy="block"
    )
    await restarted.drain()
    await _assert_current(hub, space, sessionmaker, projection)


async def test_duplicate_and_partial_batch_delivery_is_idempotent(
    hub: IdentityHub,
    space: SpaceHandle,
    sessionmaker: async_sessionmaker[AsyncSession],
    projection: Table,
) -> None:
    identity = await register(space, service="fake_tg", external_ref="duplicates")
    apply = _consumer(hub, space, sessionmaker, projection)

    async def duplicate_delivery(events: list[Event]) -> None:
        await apply(events)
        await apply(events[:1])
        await apply(events)

    hub.events.subscribe_batch(
        "external-duplicates",
        duplicate_delivery,
        types={"identity.*"},
        start="now",
        failure_policy="block",
    )
    await hub.dispatcher.run_once()
    await _snapshot(hub, space, sessionmaker, projection)
    await space.identities.set_note(identity.identity_id, "revision-2", actor="test")
    await space.identities.set_note(identity.identity_id, "revision-3", actor="test")
    await hub.dispatcher.drain()
    await _assert_current(hub, space, sessionmaker, projection)


async def test_snapshot_keyset_index_is_space_then_identity_id(db: AsyncEngine) -> None:
    async with db.connect() as connection:
        definition = (
            await connection.execute(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE schemaname = current_schema() AND tablename = 'ih_identities' "
                    "AND indexname = 'ix_ih_identities_space_id'"
                )
            )
        ).scalar_one()
    assert "(space_id, id)" in definition
