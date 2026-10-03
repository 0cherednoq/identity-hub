"""F1.1 (IMP-03): протокол идемпотентности операций — R2-D03, R1-02, R1-11.

Семантика повтора с тем же `key` (D-152, D-153):

- прошлая попытка `succeeded` → `duplicate=True`, `previous_outcome="succeeded"`;
- прошлая попытка `failed` → новая попытка, `duplicate=False`, `previous_outcome="failed"`, квота списана;
- исход неизвестен → `duplicate=True`, `previous_outcome is None`;
- тот же ключ с другим видом операции → `IdempotencyConflict`;
- ключ живёт в пределах пространства.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from tests.fixtures.api import register

from identity_hub import IdempotencyConflict, IdentityHub, SpaceHandle
from identity_hub.metrics import RecordingMetrics
from identity_hub.pool import Pool

pytestmark = pytest.mark.conformance


async def _pool(space: SpaceHandle, *refs: str) -> Pool:
    for ref in refs or ("+1",):
        await register(
            space, service="fake_tg", external_ref=ref, lifecycle="ready", session={"auth_key": "k"}
        )
    return space.pools.define("p", service="fake_tg")


async def _used(space: SpaceHandle, identity_id: UUID, kind: str) -> int:
    async with space._tx(own=True) as tx:
        windows = await tx.quota_usage(identity_id)
    return max((window.used for window in windows if window.kind == kind), default=0)


async def test_first_attempt_is_not_duplicate(space: SpaceHandle) -> None:
    pool = await _pool(space)
    async with pool.lease() as lease, lease.operation("read_channel", key="job:1") as op:
        assert (op.duplicate, op.previous_outcome) == (False, None)


async def test_same_key_in_two_spaces_is_independent(hub_under_test: IdentityHub) -> None:
    """R2-D03: ключ `order-123` второго пространства не считается повтором первого."""
    flags = []
    for _ in range(2):
        space = hub_under_test.space(f"s-{uuid4().hex[:8]}", create=True)
        pool = await _pool(space, f"+{uuid4().int % 10**9}")
        async with pool.lease() as lease, lease.operation("read_channel", key="order-123") as op:
            flags.append(op.duplicate)
            identity_id = lease.identity_id
        assert await _used(space, identity_id, "read_channel") == 1
    assert flags == [False, False]


async def test_retry_after_failure_is_a_new_attempt(space: SpaceHandle) -> None:
    """R1-02: после неуспеха повтор выполняется заново и списывает квоту."""
    pool = await _pool(space)
    async with pool.lease() as lease:
        with pytest.raises(ConnectionError):
            async with lease.operation("read_channel", key="job:1"):
                raise ConnectionError("сеть")
        async with lease.operation("read_channel", key="job:1") as retry:
            assert (retry.duplicate, retry.previous_outcome) == (False, "failed")
        async with lease.operation("read_channel", key="job:1") as again:
            assert (again.duplicate, again.previous_outcome) == (True, "succeeded")
        assert await _used(space, lease.identity_id, "read_channel") == 2


async def test_retry_after_success_is_duplicate(space: SpaceHandle) -> None:
    pool = await _pool(space)
    async with pool.lease() as lease:
        async with lease.operation("read_channel", key="job:1"):
            pass
        async with lease.operation("read_channel", key="job:1") as again:
            assert (again.duplicate, again.previous_outcome) == (True, "succeeded")
        assert await _used(space, lease.identity_id, "read_channel") == 1


async def test_unknown_outcome_is_duplicate_without_previous_outcome(space: SpaceHandle) -> None:
    """Тело отменено или процесс упал: исход неизвестен, повтор не выполняет действие вслепую."""
    pool = await _pool(space)
    async with pool.lease() as lease:
        with pytest.raises(asyncio.CancelledError):
            async with lease.operation("read_channel", key="job:1"):
                raise asyncio.CancelledError
        async with lease.operation("read_channel", key="job:1") as again:
            assert (again.duplicate, again.previous_outcome) == (True, None)
        assert await _used(space, lease.identity_id, "read_channel") == 1


async def test_same_key_with_another_operation_kind_conflicts(space: SpaceHandle) -> None:
    pool = await _pool(space)
    async with pool.lease() as lease:
        async with lease.operation("read_channel", key="job:1"):
            pass
        with pytest.raises(IdempotencyConflict) as exc:
            async with lease.operation("send_message", key="job:1"):
                pytest.fail("тело не должно выполняться")
        assert "job:1" not in str(exc.value) and "job:1" not in str(exc.value.context)
        assert await _used(space, lease.identity_id, "send_message") == 0


async def test_conflict_also_applies_after_failed_attempt(space: SpaceHandle) -> None:
    pool = await _pool(space)
    async with pool.lease() as lease:
        with pytest.raises(ConnectionError):
            async with lease.operation("read_channel", key="job:1"):
                raise ConnectionError("сеть")
        with pytest.raises(IdempotencyConflict):
            async with lease.operation("send_message", key="job:1"):
                pytest.fail("тело не должно выполняться")


async def test_retry_under_another_account_keeps_key_semantics(space: SpaceHandle) -> None:
    """D-153: аккаунт выбирает пул, поэтому повтор задачи под другим аккаунтом — не конфликт."""
    pool = await _pool(space, "+1", "+2")
    async with pool.lease() as first, pool.lease() as second:
        assert first.identity_id != second.identity_id
        with pytest.raises(ConnectionError):
            async with first.operation("read_channel", key="job:1"):
                raise ConnectionError("сеть")
        async with second.operation("read_channel", key="job:1") as retry:
            assert (retry.duplicate, retry.previous_outcome) == (False, "failed")
        async with first.operation("read_channel", key="job:1") as again:
            assert (again.duplicate, again.previous_outcome) == (True, "succeeded")
        assert await _used(space, first.identity_id, "read_channel") == 1
        assert await _used(space, second.identity_id, "read_channel") == 1


async def test_bookkeeping_failure_after_success_is_not_raised(
    hub_under_test: IdentityHub, space: SpaceHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1-11: действие на платформе выполнено — сбой учёта не превращает успех в ошибку."""
    metrics = RecordingMetrics()
    hub_under_test.metrics = metrics
    pool = await _pool(space)
    calls: list[str] = []

    async def broken(*_args: Any, **_kwargs: Any) -> None:
        raise ConnectionError("db down, token=SECRET")

    async with pool.lease() as lease:
        with monkeypatch.context() as patch:
            patch.setattr("identity_hub.operation._record_success", broken)
            async with lease.operation("read_channel", key="job:1"):
                calls.append("sent")
        assert calls == ["sent"]
        async with lease.operation("read_channel", key="job:1") as again:
            assert (again.duplicate, again.previous_outcome) == (True, None)
    failures = [
        event.labels
        for event in metrics.events
        if event.name == "identity_hub_failures_total" and event.labels["scope"] == "operation_bookkeeping"
    ]
    assert failures == [
        {
            "space": space.slug,
            "service": "fake_tg",
            "scope": "operation_bookkeeping",
            "reason": "ConnectionError",
        }
    ]


async def test_expired_key_can_be_used_again(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    """Запись ключа живёт `Retention.operation_keys`; после срока ключ свободен."""
    hub_under_test.retention = replace(hub_under_test.retention, operation_keys=timedelta(0))
    pool = await _pool(space)
    async with pool.lease() as lease:
        async with lease.operation("read_channel", key="job:1"):
            pass
        async with lease.operation("read_channel", key="job:1") as again:
            assert (again.duplicate, again.previous_outcome) == (False, None)
