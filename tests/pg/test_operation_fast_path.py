"""I6.1 (`PERF-1`): число SQL round trips успешной `lease.operation` зафиксировано регрессией."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

from identity_hub import IdentityCreate, IdentityHub, Lifecycle
from identity_hub.hub import SpaceHandle

pytestmark = pytest.mark.pg

# Бюджет round trips на одну успешную операцию без квот, pacing и ключа идемпотентности.
ENTER_BUDGET = 3


async def _leased(space: SpaceHandle) -> Any:
    await space.identities.create(
        IdentityCreate(
            service="fake_tg",
            external_ref="+79990009999",
            session={"auth_key": "k"},
            lifecycle=Lifecycle.READY,
        ),
        actor="test",
    )
    return await space.pools.define("fast", service="fake_tg").acquire()


async def test_successful_operation_keeps_round_trip_budget(
    hub: IdentityHub, db: AsyncEngine, space: SpaceHandle
) -> None:
    lease = await _leased(space)
    statements: list[str] = []

    def capture(_conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        statements.append(statement)

    async with lease.operation("read_channel"):  # прогрев: кэши схемы и план
        pass
    event.listen(db.sync_engine, "before_cursor_execute", capture)
    try:
        async with lease.operation("read_channel"):
            pass
        entered = len(statements)
    finally:
        event.remove(db.sync_engine, "before_cursor_execute", capture)
        await lease.release()

    assert entered <= ENTER_BUDGET, f"round trips: {entered}\n" + "\n".join(statements)


async def test_decay_still_applies_when_error_score_is_positive(hub: IdentityHub, space: SpaceHandle) -> None:
    """I6.1: пропуск учёта — только при нулевом счётчике ошибок; иначе затухание работает."""
    lease = await _leased(space)
    async with space._tx(own=True) as tx:
        await tx.add_error_score(lease.identity_id, 4.0)

    async with lease.operation("read_channel"):
        pass
    await lease.release()

    identity = await space.identities.get(lease.identity_id)
    assert 0 < identity.error_score < 4.0  # SUCCESS_DECAY применён


async def test_quota_exhaustion_without_savepoint_leaves_no_partial_charge(
    hub: IdentityHub, space: SpaceHandle
) -> None:
    """I6.1: быстрый путь списания одной квоты не оставляет списанного при исчерпании."""
    from datetime import timedelta

    from identity_hub import OperationRule, Quota, QuotaExhausted, Service

    hub.register_service(
        Service(
            name="fast_quota",
            title="Fast quota",
            operations={"work": OperationRule(needs_auth=False, quotas=(Quota(1, timedelta(hours=1)),))},
        )
    )
    await hub.start()
    await space.identities.create(
        IdentityCreate(service="fast_quota", external_ref="+79990008888", lifecycle=Lifecycle.READY),
        actor="test",
    )
    lease = await space.pools.define("fq", service="fast_quota").acquire(for_operation="work")

    async with lease.operation("work"):
        pass
    with pytest.raises(QuotaExhausted):
        async with lease.operation("work"):
            pass

    async with space._tx(own=True) as tx:
        windows = await tx.quota_usage(lease.identity_id)
    used = {window.kind: window.used for window in windows}
    assert used["work"] == 1  # второе списание не прошло и не осталось частичным
    await lease.release()
