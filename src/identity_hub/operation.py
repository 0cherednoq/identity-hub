"""Операция под арендой (ТЗ FR-7.1, 7.3, 7.4, 7.5, §3.9).

    async with lease.operation("send_message", key=job_id) as op:
        await gateway.send(...)

1. Проверка допустимости по свежему снимку аккаунта и fencing аренды.
2. Списание квот и pacing **в своей транзакции до вызова платформы** (G-2.1).
3. Выход без исключения — успех; с исключением — классификатор сервиса определяет вину
   и порождает наблюдение для соответствующего региона.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, cast

from identity_hub._space import logger
from identity_hub.domain.admissibility import IdentitySnapshot, evaluate
from identity_hub.domain.idempotency import OPERATION_FAMILY, namespace
from identity_hub.domain.redaction import sanitize_text
from identity_hub.domain.regions import CODE_LIMIT, RAW_STATUS_LIMIT, Command, Observation, Timer
from identity_hub.enums import AuthState, Availability, ChangeSource, SessionStatus, Standing
from identity_hub.errors import OperationNotAllowed, TransitionNotAllowed
from identity_hub.events import EventDraft
from identity_hub.metrics import record_metric
from identity_hub.protocols import Blame, ErrorClassifier, Fault
from identity_hub.quotas import charge
from identity_hub.state import DEFAULT_SLOT, apply_region_change
from identity_hub.store.base import StoreTx

if TYPE_CHECKING:
    from identity_hub.lease import Lease

__all__ = ["SUCCESS_DECAY", "Operation", "OperationOutcome", "apply_fault_effects", "operation_scope"]

SUCCESS_DECAY = 0.5

OperationOutcome = Literal["succeeded", "failed"]


class Operation:
    """Одна операция на платформе под арендой.

    С ключом `key` повтор различает исход прошлой попытки (D-152, D-153):

    - `duplicate=True`, `previous_outcome="succeeded"` — действие уже выполнено, квота не списана;
    - `duplicate=True`, `previous_outcome=None` — исход неизвестен: прошлая попытка ещё идёт,
      была отменена или процесс упал. Действие могло выполниться — сверьтесь с платформой;
    - `duplicate=False`, `previous_outcome="failed"` — прошлая попытка упала, это новая попытка;
    - `duplicate=False`, `previous_outcome=None` — первая попытка.
    """

    def __init__(
        self,
        lease: Lease,
        kind: str,
        key: str | None,
        duplicate: bool,
        paused_until: datetime | None,
        previous_outcome: OperationOutcome | None = None,
    ):
        self.lease = lease
        self.kind = kind
        self.key = key
        self.duplicate = duplicate  # ключ уже использован: квота не списана (FR-7.5)
        self.paused_until = paused_until  # pacing отправил аккаунт отдыхать после этой операции
        self.previous_outcome = previous_outcome

    async def cool_down(self, retry_after: timedelta | float) -> None:
        """Rate limit / flood-wait: availability → cooling до now + retry_after; только удлиняет (FR-7.4)."""
        seconds = retry_after.total_seconds() if isinstance(retry_after, timedelta) else float(retry_after)
        space = self.lease.pool.space
        async with space._tx(own=True) as tx:
            until = await tx.now() + timedelta(seconds=seconds)
            await _cooling(self.lease, tx, until)

    def __repr__(self) -> str:
        return (
            f"Operation(kind={self.kind!r}, lease_id={self.lease.lease_id}, duplicate={self.duplicate}, "
            f"previous_outcome={self.previous_outcome!r})"
        )


@asynccontextmanager
async def operation_scope(
    lease: Lease[Any, Any], kind: str, key: str | None = None
) -> AsyncIterator[Operation]:
    started = lease.pool.space.hub.clock.monotonic()
    lease.check()
    space = lease.pool.space
    service = lease.pool.service
    async with space._tx(own=True) as tx:
        # R1-15: fence под блокировкой строки и с живой арендой, а не одно сравнение номера (D-212)
        await tx.check_fence(lease.fence)
        identity = await space._identity(tx, lease.identity_id)
        snapshot = IdentitySnapshot(
            lifecycle=identity.lifecycle,
            standing=identity.standing,
            standing_code=identity.standing_code,
            auth_state=identity.auth_state,
            availability=identity.availability,
            availability_until=identity.availability_until,
            verified_factors=frozenset(identity.verified_factors),
            expired_factors=frozenset(await tx.expired_factors(lease.identity_id)),
        )
        blockers = evaluate(service, kind, snapshot, await tx.now())
        if blockers:
            for blocker in blockers:
                record_metric(
                    space.hub.metrics,
                    "counter",
                    "identity_hub_failures_total",
                    space=space.slug,
                    service=service.name,
                    scope="operation",
                    reason=blocker.code,
                )
            _observe_operation(lease, kind, started, "rejected")
            raise OperationNotAllowed(kind, blockers)
        result = await charge(
            space,
            tx,
            identity,
            service,
            kind,
            pacing=lease.pool.pacing,
            operation_key=key,
            lease_id=lease.lease_id,
            actor=lease.holder,
        )
    op = Operation(lease, kind, key, result.duplicate, result.paused_until, _outcome(result.previous_outcome))
    try:
        yield op
    except asyncio.CancelledError:
        _observe_operation(lease, kind, started, "cancelled")
        raise  # потеря аренды или отмена — не вина аккаунта
    except Exception as exc:
        lease._count(kind, ok=False, error=type(exc).__name__)
        await _on_failure(lease, op, exc)
        record_metric(
            space.hub.metrics,
            "counter",
            "identity_hub_failures_total",
            space=space.slug,
            service=service.name,
            scope="operation",
            reason=type(exc).__name__,
        )
        _observe_operation(lease, kind, started, "failed")
        raise
    else:
        lease._count(kind, ok=True)
        try:
            await _record_success(lease, op)
        except Exception as exc:
            # Действие на платформе уже выполнено: сбой учёта не превращает успех в ошибку (R1-11).
            # Исход ключа остаётся неизвестным — повтор получит duplicate=True и не выполнит действие.
            logger.warning(
                "операция %s выполнена, но учёт результата не записан: %s", kind, type(exc).__name__
            )
            record_metric(
                space.hub.metrics,
                "counter",
                "identity_hub_failures_total",
                space=space.slug,
                service=service.name,
                scope="operation_bookkeeping",
                reason=type(exc).__name__,
            )
        _observe_operation(lease, kind, started, "succeeded")


def _outcome(value: str | None) -> OperationOutcome | None:
    if value == "succeeded":
        return "succeeded"
    if value == "failed":
        return "failed"
    return None


async def _record_success(lease: Lease, op: Operation) -> None:
    space = lease.pool.space
    async with space._tx(own=True) as tx:
        await tx.decay_error_score(lease.identity_id, SUCCESS_DECAY)
        if op.key is not None and not op.duplicate:
            await tx.complete_idempotency_key(namespace(tx.space.id, OPERATION_FAMILY), op.key, "succeeded")


def _observe_operation(lease: Lease, kind: str, started: float, status: str) -> None:
    hub = lease.pool.space.hub
    record_metric(
        hub.metrics,
        "histogram",
        "identity_hub_operation_duration_seconds",
        max(0.0, hub.clock.monotonic() - started),
        space=lease.pool.space.slug,
        service=lease.service,
        operation=kind,
        status=status,
    )


def classify_error(classifier: ErrorClassifier, exc: BaseException, *, kind: str) -> Fault | None:
    """Вызвать классификатор; вид операции передаётся, если `classify` принимает `kind` (R2-F27)."""
    method = classifier.classify
    try:
        parameters = list(inspect.signature(method).parameters.values())
    except (TypeError, ValueError):
        parameters = []
    if any(item.name == "kind" or item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters):
        return method(exc, kind=kind)  # type: ignore[call-arg]
    return method(exc)


async def _on_failure(lease: Lease, op: Operation, exc: Exception) -> None:
    """Классификатор определяет вину и порождает наблюдение (FR-7.3); сбой обработки не маскирует ошибку."""
    service = lease.pool.service
    fault: Fault | None = None
    if service.error_classifier is not None:
        try:
            # словарь приведён к классификатору в Service.__post_init__
            fault = classify_error(cast(ErrorClassifier, service.error_classifier), exc, kind=op.kind)
        except Exception:
            logger.exception("ErrorClassifier сервиса %s упал", service.name)
    fault = fault or Fault(Blame.REQUEST)
    # текст классификатора уходит во внешние события и карточку — без секретов и в размер колонок
    fault = replace(
        fault,
        code=sanitize_text(fault.code, limit=CODE_LIMIT),
        raw_status=sanitize_text(fault.raw_status, limit=RAW_STATUS_LIMIT),
    )
    space = lease.pool.space
    try:
        async with space._tx(own=True) as tx:
            if op.key is not None and not op.duplicate:
                await tx.complete_idempotency_key(namespace(tx.space.id, OPERATION_FAMILY), op.key, "failed")
            await apply_fault_effects(lease, tx, fault)
            # Blame.PROXY — здоровье прокси (ниже, своей транзакцией); Blame.REQUEST — состояние не меняется
            await space._emit(
                tx,
                EventDraft(
                    "operation.failed",
                    actor=lease.holder,
                    service=service.name,
                    identity_id=lease.identity_id,
                    lease_id=lease.lease_id,
                    data={
                        "kind": op.kind,
                        "blame": str(fault.blame),
                        "code": fault.code,
                        "error": type(exc).__name__,
                    },
                ),
            )
    except Exception:
        logger.exception("не удалось записать последствия ошибки операции %s", op.kind)
    if fault.blame is Blame.PROXY and lease.proxy_id is not None:
        try:
            await space._report_proxy_failure(  # type: ignore[attr-defined]
                lease.proxy_id, weight=fault.weight, actor=lease.holder, raw_status=fault.raw_status
            )
        except Exception:
            logger.exception("не удалось учесть ошибку прокси %s", lease.proxy_id)


async def apply_fault_effects(lease: Lease, tx: StoreTx, fault: Fault) -> Fault:
    """Применить последствия безопасного `Fault` в переданной транзакции (D-310)."""
    fault = replace(
        fault,
        code=sanitize_text(fault.code, limit=CODE_LIMIT),
        raw_status=sanitize_text(fault.raw_status, limit=RAW_STATUS_LIMIT),
    )
    space = lease.pool.space
    service = lease.pool.service
    now = await tx.now()
    if fault.blame is Blame.IDENTITY:
        score = await tx.add_error_score(lease.identity_id, fault.weight)
        if fault.retry_after is not None:
            await _cooling(lease, tx, now + fault.retry_after)
        if score >= service.quarantine_threshold:
            await _set_availability(lease, tx, Command(Availability.QUARANTINED, actor=lease.holder))
    elif fault.blame is Blame.SESSION_INVALID:
        await tx.set_session_status(lease.identity_id, DEFAULT_SLOT, SessionStatus.INVALID)
        await apply_region_change(
            space,
            tx,
            lease.identity_id,
            "auth",
            DEFAULT_SLOT,
            Observation(
                AuthState.SESSION_INVALID,
                observed_at=now,
                source=ChangeSource.ERROR,
                raw_status=fault.raw_status,
                code=fault.code,
                actor=lease.holder,
            ),
            lease_id=lease.lease_id,
        )
    elif fault.blame is Blame.IDENTITY_BANNED:
        await apply_region_change(
            space,
            tx,
            lease.identity_id,
            "standing",
            "",
            Observation(
                fault.standing or Standing.BANNED,
                observed_at=now,
                source=ChangeSource.ERROR,
                raw_status=fault.raw_status,
                code=fault.code,
                until=fault.until,
                actor=lease.holder,
            ),
            lease_id=lease.lease_id,
        )
    return fault


async def _cooling(lease: Lease, tx: StoreTx, until: datetime) -> None:
    await _set_availability(lease, tx, Command(Availability.COOLING, actor=lease.holder, until=until))


async def _set_availability(lease: Lease, tx: StoreTx, command: Command) -> None:
    space = lease.pool.space
    try:
        await apply_region_change(
            space, tx, lease.identity_id, "availability", "", command, lease_id=lease.lease_id
        )
    except TransitionNotAllowed:
        # истёкший paused/cooling ещё не снят лениво — снять таймером и повторить; иначе состояние тяжелее
        await apply_region_change(
            space, tx, lease.identity_id, "availability", "", Timer(), lease_id=lease.lease_id
        )
        try:
            await apply_region_change(
                space, tx, lease.identity_id, "availability", "", command, lease_id=lease.lease_id
            )
        except TransitionNotAllowed:
            return
