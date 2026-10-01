"""F1.1 (IMP-03): гонка за ключ идемпотентности и записи, созданные до протокола."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub.domain.idempotency import OPERATION_FAMILY, fingerprint, namespace
from identity_hub.errors import IdempotencyConflict
from identity_hub.hub import SpaceHandle
from identity_hub.postgres.models import OperationKeyRecord, QuotaUsageRecord
from identity_hub.store.base import IdempotencyClaim, IdempotencyRequest
from tests.pg.helpers import make_identity

pytestmark = pytest.mark.pg


async def _request(space: SpaceHandle, key: str, kind: str = "read_channel") -> IdempotencyRequest:
    ref = await space.ref()
    return IdempotencyRequest(
        namespace=namespace(ref.id, OPERATION_FAMILY),
        key=key,
        operation=kind,
        fingerprint=fingerprint(OPERATION_FAMILY, {"service": "fake_tg", "kind": kind}),
        ttl=timedelta(days=1),
    )


async def _claim(space: SpaceHandle, request: IdempotencyRequest) -> IdempotencyClaim:
    async with space._tx(own=True) as tx:
        return await tx.claim_idempotency_key(request)


async def _wait_blocked(sessionmaker: async_sessionmaker[AsyncSession], task: asyncio.Task[object]) -> None:
    """Дождаться, пока конкурент встанет на блокировку строки ключа (барьер без sleep-порядка)."""
    # pg_locks читается без кэша статистики, в отличие от pg_stat_activity внутри одной транзакции
    blocked = text("SELECT count(*) FROM pg_locks WHERE NOT granted")
    async with sessionmaker() as session:
        for _ in range(500):
            assert not task.done(), "конкурент не дождался незакоммиченного победителя"
            if (await session.execute(blocked)).scalar_one():
                return
            await asyncio.sleep(0.01)
    pytest.fail("конкурент не встал на блокировку ключа")


@pytest.mark.parametrize("winner_commits", [True, False], ids=["commit", "rollback"])
async def test_uncommitted_winner_holds_competitor(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession], winner_commits: bool
) -> None:
    request = await _request(space, "job:race")
    claimed, release = asyncio.Event(), asyncio.Event()

    async def winner() -> None:
        async with space._tx(own=True) as tx:
            assert (await tx.claim_idempotency_key(request)).claimed
            claimed.set()
            await release.wait()
            if not winner_commits:
                raise RuntimeError("rollback")

    first = asyncio.create_task(winner())
    await claimed.wait()  # победитель вставил строку, но не закоммитил
    second = asyncio.create_task(_claim(space, request))
    await _wait_blocked(sessionmaker, second)
    release.set()
    if winner_commits:
        await first
        competitor = await second
        assert (competitor.claimed, competitor.previous_outcome) == (False, None)
    else:
        with pytest.raises(RuntimeError):
            await first
        competitor = await second
        assert (competitor.claimed, competitor.attempt) == (True, 1)


async def test_two_parallel_operations_with_one_key_charge_once(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    for _ in range(2):
        await make_identity(space)
    pool = space.pools.define("p", service="fake_tg")
    barrier = asyncio.Barrier(2)

    async def run() -> bool:
        async with pool.lease() as lease:
            await barrier.wait()
            async with lease.operation("read_channel", key="job:1") as op:
                return op.duplicate

    flags = await asyncio.gather(run(), run())
    assert sorted(flags) == [False, True]
    async with sessionmaker() as session:
        assert (await session.execute(select(func.sum(QuotaUsageRecord.used)))).scalar_one() == 1


async def _legacy_row(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession], outcome: str | None
) -> UUID:
    """Строка в том виде, в каком её оставляет миграция 0009 для ключей прежних версий."""
    identity_id = await make_identity(space)
    ref = await space.ref()
    async with sessionmaker.begin() as session:
        await session.execute(
            insert(OperationKeyRecord).values(
                namespace=namespace(ref.id, OPERATION_FAMILY),
                key="job:legacy",
                identity_id=identity_id,
                space_id=ref.id,
                operation="read_channel",
                fingerprint_version=0,
                fingerprint=None,
                attempt=1,
                outcome=outcome,
                expires_at=func.now() + timedelta(days=7),
            )
        )
    return identity_id


async def test_legacy_key_is_still_a_duplicate(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await _legacy_row(space, sessionmaker, "succeeded")
    replay = await _claim(space, await _request(space, "job:legacy"))
    assert (replay.claimed, replay.previous_outcome) == (False, "succeeded")
    with pytest.raises(IdempotencyConflict):
        await _claim(space, await _request(space, "job:legacy", kind="send_message"))


async def test_legacy_failed_key_is_retried_and_upgraded(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await _legacy_row(space, sessionmaker, "failed")
    request = await _request(space, "job:legacy")
    retry = await _claim(space, request)
    assert (retry.claimed, retry.attempt, retry.previous_outcome) == (True, 2, "failed")
    async with sessionmaker() as session:
        row = (await session.execute(select(OperationKeyRecord))).scalar_one()
    assert (row.fingerprint_version, row.fingerprint, row.outcome) == (1, request.fingerprint, None)
