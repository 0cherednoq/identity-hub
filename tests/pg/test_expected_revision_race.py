"""IMP-07 (I1.2b): две сессии с одной ревизией — один победитель; savepoint и откат."""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub import IdentityCreate, RevisionConflict
from identity_hub.hub import SpaceHandle

pytestmark = [pytest.mark.anyio, pytest.mark.pg]


async def _create(space: SpaceHandle, ref: str) -> tuple[UUID, int]:
    created = await space.identities.create(IdentityCreate(service="fake_tg", external_ref=ref), actor="test")
    return created.id, created.revision


async def _wait_blocked(sessionmaker: async_sessionmaker[AsyncSession], task: asyncio.Task[object]) -> None:
    """Дождаться, пока конкурент встанет на блокировку строки identity (барьер без sleep-порядка)."""
    blocked = text("SELECT count(*) FROM pg_locks WHERE NOT granted")
    async with sessionmaker() as session:
        for _ in range(500):
            assert not task.done(), "конкурент не дождался незакоммиченного победителя"
            if (await session.execute(blocked)).scalar_one():
                return
            await asyncio.sleep(0.01)
    pytest.fail("конкурент не встал на блокировку identity")


@pytest.mark.parametrize("winner_commits", [True, False], ids=["commit", "rollback"])
async def test_two_sessions_one_winner(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession], winner_commits: bool
) -> None:
    identity_id, revision = await _create(space, "+race")
    updated, release = asyncio.Event(), asyncio.Event()

    async def winner() -> None:
        async with sessionmaker() as session, session.begin():
            await space.using(session).identities.update(
                identity_id, labels={"by": "a"}, actor="a", expected_revision=revision
            )
            updated.set()
            await release.wait()
            if not winner_commits:
                raise RuntimeError("rollback")

    async def loser() -> object:
        async with sessionmaker() as session, session.begin():
            return await space.using(session).identities.update(
                identity_id, labels={"by": "b"}, actor="b", expected_revision=revision
            )

    first = asyncio.create_task(winner())
    await updated.wait()  # победитель изменил строку, но не закоммитил
    second = asyncio.create_task(loser())
    await _wait_blocked(sessionmaker, second)
    release.set()
    if winner_commits:
        await first
        with pytest.raises(RevisionConflict):
            await second
        final = await space.identities.get(identity_id)
        assert (final.labels, final.revision) == ({"by": "a"}, revision + 1)
    else:
        with pytest.raises(RuntimeError):
            await first
        await second  # откат победителя вернул ревизию: второй применился
        final = await space.identities.get(identity_id)
        assert (final.labels, final.revision) == ({"by": "b"}, revision + 1)


async def test_conflict_in_savepoint_keeps_outer_transaction(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    identity_id, revision = await _create(space, "+savepoint")
    other_id, _ = await _create(space, "+other")

    async with sessionmaker() as session, session.begin():
        bound = space.using(session)
        with pytest.raises(RevisionConflict):
            await bound.identities.add_tags(identity_id, ["x"], actor="ops", expected_revision=revision + 5)
        # savepoint откатился, внешняя транзакция жива: следующая команда в ней проходит
        await bound.identities.add_tags(other_id, ["y"], actor="ops")

    assert (await space.identities.get(identity_id)).revision == revision
    assert (await space.identities.get(other_id)).tags == {"y"}


async def test_rolled_back_outer_transaction_keeps_revision(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    identity_id, revision = await _create(space, "+rollback")

    async with sessionmaker() as session:
        transaction = await session.begin()
        await space.using(session).identities.set_note(
            identity_id, "draft", actor="ops", expected_revision=revision
        )
        await transaction.rollback()

    current = await space.identities.get(identity_id)
    assert (current.note, current.revision) == (None, revision)
    await space.identities.set_note(identity_id, "final", actor="ops", expected_revision=revision)
