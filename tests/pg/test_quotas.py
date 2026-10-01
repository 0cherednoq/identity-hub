"""P4.1: квоты, pacing, ключи операций (ТЗ FR-7.2, 7.5, G-2.1, G-2.2)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub.errors import QuotaExhausted
from identity_hub.hub import SpaceHandle
from identity_hub.postgres.models import EventRecord, IdentityRecord, OperationKeyRecord, QuotaUsageRecord
from identity_hub.quotas import ChargeResult, charge
from identity_hub.service import Pacing, Quota
from tests.fixtures.services import FAKE_TG
from tests.pg.helpers import make_identity

pytestmark = pytest.mark.pg


async def _charge(space: SpaceHandle, identity_id: Any, kind: str, **kwargs: Any) -> ChargeResult:
    async with space._tx(own=True) as tx:
        identity = await space._identity(tx, identity_id)
        return await charge(space, tx, identity, FAKE_TG, kind, **kwargs)


async def _all(sm: async_sessionmaker[AsyncSession], model: Any) -> list[Any]:
    async with sm() as s:
        return list((await s.execute(select(model))).scalars())


async def test_race_for_last_units(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    identity_id = await make_identity(space)
    results = await asyncio.gather(
        *(_charge(space, identity_id, "send_message") for _ in range(30)), return_exceptions=True
    )
    ok = [r for r in results if isinstance(r, ChargeResult)]
    exhausted = [r for r in results if isinstance(r, QuotaExhausted)]
    assert len(ok) == 20  # лимит в час
    assert len(exhausted) == 10
    assert exhausted[0].window_seconds == 3600 and exhausted[0].retry_at is not None
    rows = {(r.kind, r.window_seconds): r.used for r in await _all(sessionmaker, QuotaUsageRecord)}
    assert rows == {("send_message", 3600): 20, ("send_message", 86400): 20}  # сутки не пересписаны (G-2.2)
    events = [e for e in await _all(sessionmaker, EventRecord) if e.type == "identity.quota.exhausted"]
    assert len(events) == 1  # ровно раз за окно
    assert events[0].data["kind"] == "send_message" and events[0].data["window_seconds"] == 3600


async def test_operation_key_is_idempotent(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    identity_id = await make_identity(space)
    first = await _charge(space, identity_id, "read_channel", operation_key="op-1")
    again = await _charge(space, identity_id, "read_channel", operation_key="op-1")
    assert (first.duplicate, again.duplicate) == (False, True)
    (usage,) = await _all(sessionmaker, QuotaUsageRecord)
    assert usage.used == 1
    (key,) = await _all(sessionmaker, OperationKeyRecord)
    assert (key.operation, key.outcome, key.identity_id) == ("read_channel", None, identity_id)
    assert key.namespace == f"{key.space_id}:operation"
    async with space._tx(own=True) as tx:
        await tx.complete_idempotency_key(key.namespace, "op-1", "succeeded")
    assert (await _all(sessionmaker, OperationKeyRecord))[0].outcome == "succeeded"


async def test_exhausted_quota_does_not_consume_key(space: SpaceHandle) -> None:
    identity_id = await make_identity(space)
    for i in range(20):
        await _charge(space, identity_id, "send_message", operation_key=f"k{i}")
    with pytest.raises(QuotaExhausted):
        await _charge(space, identity_id, "send_message", operation_key="late")
    with pytest.raises(QuotaExhausted):  # ключ не занят — повтор снова упирается в квоту, а не в дубликат
        await _charge(space, identity_id, "send_message", operation_key="late")


async def test_usage_and_no_quota_kinds(space: SpaceHandle) -> None:
    identity_id = await make_identity(space)
    await _charge(space, identity_id, "probe")  # без квот — ничего не пишет
    await _charge(space, identity_id, "read_channel")
    async with space._tx(own=True) as tx:
        usage = await tx.quota_usage(identity_id)
    assert [(u.kind, u.window_seconds, u.used) for u in usage] == [("read_channel", 3600, 1)]


async def test_pacing_pauses_and_resets(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    identity_id = await make_identity(space)
    pacing = Pacing(work=Quota(3, timedelta(hours=1)), pause=(15, 25))
    results = [await _charge(space, identity_id, "read_channel", pacing=pacing) for _ in range(3)]
    assert [r.paused_until is not None for r in results] == [False, False, True]
    async with sessionmaker() as s:
        row = (await s.execute(select(IdentityRecord))).scalar_one()
        pacing_row = (
            await s.execute(select(QuotaUsageRecord).where(QuotaUsageRecord.kind == "__pacing__"))
        ).scalar_one()
    assert row.availability == "paused"
    assert row.availability_until == results[2].paused_until
    assert pacing_row.used == 0
