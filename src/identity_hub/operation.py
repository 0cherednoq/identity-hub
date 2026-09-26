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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from identity_hub._space import logger
from identity_hub.domain.admissibility import IdentitySnapshot, evaluate
from identity_hub.domain.regions import Command, Observation, Timer
from identity_hub.enums import AuthState, Availability, ChangeSource, SessionStatus, Standing
from identity_hub.errors import OperationNotAllowed, StaleFencing, TransitionNotAllowed
from identity_hub.events import EventDraft
from identity_hub.protocols import Blame, Fault
from identity_hub.quotas import charge
from identity_hub.state import DEFAULT_SLOT, apply_region_change
from identity_hub.store.base import StoreTx

if TYPE_CHECKING:
    from identity_hub.lease import Lease

__all__ = ["SUCCESS_DECAY", "Operation", "operation_scope"]

SUCCESS_DECAY = 0.5


class Operation:
    """Одна операция на платформе под арендой."""

    def __init__(
        self, lease: Lease, kind: str, key: str | None, duplicate: bool, paused_until: datetime | None
    ):
        self.lease = lease
        self.kind = kind
        self.key = key
        self.duplicate = duplicate  # ключ уже использован: квота не списана (FR-7.5)
        self.paused_until = paused_until  # pacing отправил аккаунт отдыхать после этой операции

    async def cool_down(self, retry_after: timedelta | float) -> None:
        """Rate limit / flood-wait: availability → cooling до now + retry_after; только удлиняет (FR-7.4)."""
        seconds = retry_after.total_seconds() if isinstance(retry_after, timedelta) else float(retry_after)
        space = self.lease.pool.space
        async with space._tx(own=True) as tx:
            until = await tx.now() + timedelta(seconds=seconds)
            await _cooling(self.lease, tx, until)

    def __repr__(self) -> str:
        return f"Operation(kind={self.kind!r}, lease_id={self.lease.lease_id}, duplicate={self.duplicate})"


@asynccontextmanager
async def operation_scope(lease: Lease, kind: str, key: str | None = None) -> AsyncIterator[Operation]:
    lease.check()
    space = lease.pool.space
    service = lease.pool.service
    async with space._tx(own=True) as tx:
        identity = await space._identity(tx, lease.identity_id)
        if identity.fencing_seq != lease.fencing:
            raise StaleFencing(lease.identity_id, lease.fencing, identity.fencing_seq)
        snapshot = IdentitySnapshot(
            lifecycle=identity.lifecycle,
            standing=identity.standing,
            standing_code=identity.standing_code,
            auth_state=identity.auth_state,
            availability=identity.availability,
            availability_until=identity.availability_until,
            verified_factors=frozenset(identity.verified_factors),
        )
        blockers = evaluate(service, kind, snapshot, await tx.now())
        if blockers:
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
    op = Operation(lease, kind, key, result.duplicate, result.paused_until)
    try:
        yield op
    except asyncio.CancelledError:
        raise  # потеря аренды или отмена — не вина аккаунта
    except Exception as exc:
        lease._count(kind, ok=False, error=f"{type(exc).__name__}: {exc}"[:2000])
        await _on_failure(lease, op, exc)
        raise
    else:
        lease._count(kind, ok=True)
        async with space._tx(own=True) as tx:
            await tx.decay_error_score(lease.identity_id, SUCCESS_DECAY)
            if key is not None and not op.duplicate:
                await tx.finish_operation_key(key, "succeeded")


async def _on_failure(lease: Lease, op: Operation, exc: Exception) -> None:
    """Классификатор определяет вину и порождает наблюдение (FR-7.3); сбой обработки не маскирует ошибку."""
    service = lease.pool.service
    fault: Fault | None = None
    if service.error_classifier is not None:
        try:
            fault = service.error_classifier.classify(exc)
        except Exception:
            logger.exception("ErrorClassifier сервиса %s упал", service.name)
    fault = fault or Fault(Blame.REQUEST)
    space = lease.pool.space
    try:
        async with space._tx(own=True) as tx:
            now = await tx.now()
            if op.key is not None and not op.duplicate:
                await tx.finish_operation_key(op.key, "failed")
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
                        actor=lease.holder,
                    ),
                    lease_id=lease.lease_id,
                )
            # Blame.PROXY — здоровье прокси (P5.1); Blame.REQUEST — состояние не меняется
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
