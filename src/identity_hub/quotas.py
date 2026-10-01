"""Квоты и pacing (ТЗ FR-7.1, 7.2, 7.5, 7.6, G-2).

Списание фиксируется в собственной транзакции библиотеки до вызова платформы и не откатывается
транзакцией пользователя (G-2.1). Все окна вида списываются атомарно (G-2.2). Сброса квот нет (FR-7.6).
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from identity_hub._space import SpaceBase
from identity_hub.domain.idempotency import OPERATION_FAMILY, fingerprint, namespace
from identity_hub.domain.regions import Command, Timer
from identity_hub.enums import Availability
from identity_hub.errors import QuotaExhausted, TransitionNotAllowed
from identity_hub.events import EventDraft
from identity_hub.service import Pacing, Service
from identity_hub.state import apply_region_change
from identity_hub.store.base import IdempotencyRequest, IdentityRow, QuotaCharge, StoreTx

__all__ = ["PACING_KIND", "ChargeResult", "charge"]

PACING_KIND = "__pacing__"


@dataclass(frozen=True, slots=True)
class ChargeResult:
    duplicate: bool = False  # ключ операции уже использован — квота не списана (FR-7.5)
    paused_until: datetime | None = None  # pacing: аккаунт отправлен отдыхать
    previous_outcome: str | None = None  # исход прошлой попытки с тем же ключом (D-153)


async def charge(
    space: SpaceBase,
    tx: StoreTx,
    identity: IdentityRow,
    service: Service,
    kind: str,
    *,
    pacing: Pacing | None = None,
    operation_key: str | None = None,
    lease_id: UUID | None = None,
    actor: str = "system",
) -> ChargeResult:
    """Списать квоты вида `kind` и pacing в транзакции `tx`. Исчерпание — QuotaExhausted."""
    rule = service.rule(kind)
    charges = [QuotaCharge(kind, q.window_seconds, q.origin, q.limit) for q in rule.quotas]
    request = None
    if operation_key is not None:
        request = IdempotencyRequest(
            namespace=namespace(tx.space.id, OPERATION_FAMILY),
            key=operation_key,
            operation=kind,
            fingerprint=fingerprint(OPERATION_FAMILY, {"service": service.name, "kind": kind}),
            ttl=space.hub.retention.operation_keys,
            owner_identity_id=identity.id,
        )
    outcome = await tx.charge_quotas(identity.id, charges, idempotency=request)
    previous_outcome = outcome.claim.previous_outcome if outcome.claim is not None else None
    if outcome.duplicate:
        return ChargeResult(duplicate=True, previous_outcome=previous_outcome)
    if outcome.exhausted is not None:
        window = outcome.exhausted
        raise QuotaExhausted(
            kind, window.window_seconds, window.window_start + timedelta(seconds=window.window_seconds)
        )
    if outcome.reached:
        await space._emit(
            tx,
            *(
                EventDraft(
                    "identity.quota.exhausted",
                    actor=actor,
                    service=service.name,
                    identity_id=identity.id,
                    lease_id=lease_id,
                    data={
                        "kind": w.kind,
                        "window_seconds": w.window_seconds,
                        "window_start": w.window_start,
                        "limit": w.used,
                        "resets_at": w.window_start + timedelta(seconds=w.window_seconds),
                    },
                )
                for w in outcome.reached
            ),
        )
    paused_until = None
    if pacing is not None:
        work = pacing.work
        pacing_charge = QuotaCharge(PACING_KIND, work.window_seconds, work.origin, work.limit)
        if await tx.bump_pacing(identity.id, pacing_charge):
            lo, hi = pacing.pause
            paused_until = await tx.now() + timedelta(minutes=random.uniform(lo, hi))
            await _pause(space, tx, identity.id, paused_until, actor, lease_id)
    return ChargeResult(paused_until=paused_until, previous_outcome=previous_outcome)


async def _pause(
    space: SpaceBase, tx: StoreTx, identity_id: UUID, until: datetime, actor: str, lease_id: UUID | None
) -> None:
    command = Command(Availability.PAUSED, actor=actor, until=until, reason="pacing")
    try:
        await apply_region_change(space, tx, identity_id, "availability", "", command, lease_id=lease_id)
    except TransitionNotAllowed:
        # истёкший cooling ещё не снят лениво — сначала таймер, затем пауза; иначе (карантин) не трогаем
        await apply_region_change(space, tx, identity_id, "availability", "", Timer(), lease_id=lease_id)
        try:
            await apply_region_change(space, tx, identity_id, "availability", "", command, lease_id=lease_id)
        except TransitionNotAllowed:
            return
