"""Верификации аккаунта в хранилище (ТЗ FR-10, §3.5) и требования платформы (Stripe-подобные).

Состояние фактора — строка ih_region_states (region='verification', key=factor), данные —
ih_verifications. Переход, `verified_factors` на аккаунте (FR-10.5) и событие — одной транзакцией.
Evidence шифруется и сохраняется при `start`, до обращения к платформе (FR-10.4).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from pydantic import BaseModel

from identity_hub._space import SpaceBase
from identity_hub.crypto import StoredPayload, associated_data
from identity_hub.domain.data import validate_model
from identity_hub.domain.regions import Observation
from identity_hub.domain.verification import (
    VerificationDecision,
    VerificationFlow,
    VerificationStatus,
    achieved_tiers,
    effective_factors,
    verified_factors_after,
)
from identity_hub.enums import ChangeSource
from identity_hub.errors import ConcurrentModification, ValidationFailed
from identity_hub.events import EventDraft
from identity_hub.protocols import ProbeResult
from identity_hub.store.base import IdentityRow, StoreTx

if TYPE_CHECKING:
    from identity_hub.lease import Lease

__all__ = ["RequirementsMixin", "Verification", "VerificationsMixin", "apply_verification"]

MAX_RETRIES = 5
Step = Callable[[VerificationFlow, VerificationStatus, frozenset[str], datetime], VerificationDecision]


async def apply_verification(
    space: SpaceBase,
    tx: StoreTx,
    identity_id: UUID,
    factor: str,
    step: Step,
    *,
    actor: str,
    lease_id: UUID | None = None,
    evidence: StoredPayload | None = None,
) -> VerificationDecision:
    """Шаг машины фактора; регион, ih_verifications, verified_factors и события — в `tx`."""
    now = await tx.now()
    # verified_factors пересчитывается из прочитанной строки: два фактора параллельно не должны
    # затирать друг друга (R2-D02)
    await tx.lock_identities([identity_id])
    for _ in range(MAX_RETRIES):
        identity = await space._identity(tx, identity_id)
        service = space.hub.service(identity.service)
        flow = VerificationFlow(service.verification(factor))
        region = await tx.load_region(identity_id, "verification", factor)
        row = await tx.load_verification(identity_id, factor)
        status = VerificationStatus(
            region=region or flow.initial(identity.created_at).region,
            attempts=row.attempts if row else 0,
            started_at=row.started_at if row else None,
            submitted_at=row.submitted_at if row else None,
            verified_at=row.verified_at if row else None,
        )
        verified = effective_factors(identity.verified_factors, service.factor_names)
        tiers_before = achieved_tiers(verified, service.tiers)
        decision = step(flow, status, verified, now)
        if not decision.changed and evidence is None:
            return decision
        region_changed = decision.region is not None and decision.region.changed
        if region_changed and not await tx.save_region(
            identity_id, "verification", factor, region, decision.status.region
        ):
            continue
        new = decision.status
        await tx.save_verification(
            identity_id,
            factor,
            {
                "attempts": new.attempts,
                "started_at": new.started_at,
                "submitted_at": new.submitted_at,
                "verified_at": new.verified_at,
                "expires_at": new.expires_at,
            },
            evidence,
        )
        factors = sorted(verified_factors_after(identity.verified_factors, factor, new.state))
        tiers_after = achieved_tiers(factors, service.tiers)
        if factors != sorted(identity.verified_factors):
            tier_rank = max(
                (index for index, tier in enumerate(service.tiers) if tier.factors <= frozenset(factors)),
                default=None,
            )
            await tx.update_identity(
                identity_id,
                {"verified_factors": factors, "verification_tier_rank": tier_rank},
            )
        actor_name = (
            decision.region.state.changed_by
            if decision.region is not None and decision.region.state.changed_by
            else actor
        )
        drafts = [
            EventDraft(
                "identity.tier.reached",
                actor=actor_name,
                data={"tier": tier, "factor": factor},
                service=identity.service,
                identity_id=identity_id,
                lease_id=lease_id,
            )
            for tier in sorted(tiers_after - tiers_before)
        ]
        drafts.extend(
            EventDraft(
                "identity.tier.lost",
                actor=actor_name,
                data={"tier": tier, "factor": factor},
                service=identity.service,
                identity_id=identity_id,
                lease_id=lease_id,
            )
            for tier in sorted(tiers_before - tiers_after)
        )
        if decision.region is not None and decision.region.events:
            drafts[:0] = [
                EventDraft(
                    e.type,
                    actor=decision.region.state.changed_by or actor,
                    data={**e.data, "attempts": new.attempts},
                    severity=e.severity,
                    service=identity.service,
                    identity_id=identity_id,
                    lease_id=lease_id,
                )
                for e in decision.region.events
            ]
        if drafts:
            await space._emit(
                tx,
                *drafts,
            )
        return decision
    raise ConcurrentModification("verification", f"{identity_id}/{factor}")


class Verification:
    """Фактор верификации под арендой: `lease.verification("kyc_id")`."""

    def __init__(self, lease: Lease[Any, Any], factor: str) -> None:
        self.lease = lease
        self.factor = factor
        self.spec = lease.pool.service.verification(factor)  # ValidationFailed для неизвестного

    async def _run(self, step: Step, evidence: StoredPayload | None = None) -> VerificationStatus:
        self.lease.check()
        space = self.lease.pool.space
        async with space._tx(own=True) as tx:
            await self.lease._check_fencing(tx)
            decision = await apply_verification(
                space,
                tx,
                self.lease.identity_id,
                self.factor,
                step,
                actor=self.lease.holder,
                lease_id=self.lease.lease_id,
                evidence=evidence,
            )
        return decision.status

    async def status(self) -> VerificationStatus:
        return await self._run(lambda flow, status, verified, now: VerificationDecision(status, status, None))

    async def start(self, evidence: BaseModel | Mapping[str, Any] | None = None) -> VerificationStatus:
        """Начать или возобновить (FR-10.3); evidence сохраняется до вызова платформы (FR-10.4)."""
        payload = None
        if evidence is not None:
            if self.spec.evidence is None:
                raise ValidationFailed(f"верификация {self.factor} не принимает evidence")
            model = validate_model(self.spec.evidence, evidence, "evidence")
            aad = associated_data("ih_verifications", self.lease.identity_id, self.factor)
            payload = self.lease.pool.space.hub._seal_stored_model(model, aad)
        actor = self.lease.holder
        return await self._run(lambda flow, s, verified, now: flow.start(s, verified, now, actor), payload)

    async def submit(self) -> VerificationStatus:
        actor = self.lease.holder
        return await self._run(lambda flow, s, verified, now: flow.submit(s, now, actor))

    async def approve(self) -> VerificationStatus:
        actor = self.lease.holder
        return await self._run(lambda flow, s, verified, now: flow.approve(s, now, actor))

    async def reject(self, code: str) -> VerificationStatus:
        actor = self.lease.holder
        return await self._run(lambda flow, s, verified, now: flow.reject(s, code, now, actor))

    async def abandon(self) -> VerificationStatus:
        actor = self.lease.holder
        return await self._run(lambda flow, s, verified, now: flow.abandon(s, now, actor))

    async def reconcile(
        self, observed: ProbeResult | str | None, *, raw_status: str | None = None
    ) -> VerificationStatus:
        """Наблюдение платформы о факторе (FR-9.1, 9.5): verified → missing нормализуется в revoked."""
        result = (
            observed
            if isinstance(observed, ProbeResult)
            else ProbeResult(state=observed, raw_status=raw_status)
        )
        actor = self.lease.holder

        def step(
            flow: VerificationFlow, s: VerificationStatus, verified: frozenset[str], now: datetime
        ) -> VerificationDecision:
            obs = Observation(
                result.state,
                observed_at=result.observed_at or now,
                source=ChangeSource.PROBE,
                raw_status=result.raw_status,
                code=result.code,
                until=result.until,
                actor=actor,
            )
            return flow.reconcile(s, obs, now)

        return await self._run(step)

    async def evidence(self) -> BaseModel | None:
        """Расшифрованные доказательства — только под арендой."""
        self.lease.check()
        if self.spec.evidence is None:
            return None
        async with self.lease.pool.space._tx(own=True) as tx:
            row = await tx.load_verification(self.lease.identity_id, self.factor)
        if row is None or row.evidence is None:
            return None
        aad = associated_data("ih_verifications", self.lease.identity_id, self.factor)
        return self.lease.pool.space.hub._open_stored_model(self.spec.evidence, row.evidence, aad)


class VerificationsMixin(SpaceBase):
    async def _verification_status(self, identity_id: UUID, factor: str) -> VerificationStatus:
        """Состояние фактора без аренды (админка, read-модели)."""
        async with self._tx(own=True) as tx:
            return (
                await apply_verification(
                    self,
                    tx,
                    identity_id,
                    factor,
                    lambda flow, status, verified, now: VerificationDecision(status, status, None),
                    actor="reader",
                )
            ).status

    async def _review_verification(
        self,
        identity_id: UUID,
        factor: str,
        action: Literal["approve", "reject", "abandon", "reset_attempts"],
        *,
        actor: str,
        reason: str,
        code: str | None = None,
    ) -> VerificationStatus:
        """Apply a human verification decision without exposing encrypted evidence."""
        if not actor.strip() or not reason.strip():
            raise ValidationFailed("actor and reason are required for manual verification")

        def step(
            flow: VerificationFlow,
            status: VerificationStatus,
            verified: frozenset[str],
            now: datetime,
        ) -> VerificationDecision:
            del verified
            if action == "approve":
                return flow.approve(status, now, actor, reason)
            if action == "reject":
                if not code:
                    raise ValidationFailed("verification reject code is required")
                return flow.reject(status, code, now, actor, reason)
            if action == "reset_attempts":
                return flow.reset_attempts(status)
            if action != "abandon":
                raise ValidationFailed("verification review: approve, reject, abandon или reset_attempts")
            return flow.abandon(status, now, actor, reason)

        async with self._tx() as tx:
            return (
                await apply_verification(
                    self,
                    tx,
                    identity_id,
                    factor,
                    step,
                    actor=actor,
                )
            ).status

    async def _expire_verifications(self, *, limit: int = 100) -> int:
        """Истёкшие сроки: verified → expired, pending → missing (ТЗ §8.9, обслуживание)."""
        async with self._tx(own=True) as tx:
            due = await tx.due_regions("verification", limit)
        changed = 0
        known = {service.name for service in self.hub.services}
        for identity_id, factor in due:
            async with self._tx(own=True) as tx:
                identity = await tx.get_identity(identity_id)
                if identity is None or identity.service not in known:
                    continue  # сервис убран из кода: его аккаунт не останавливает остальных (R2-F06)
                if factor not in self.hub.service(identity.service).factor_names:
                    continue  # фактор удалён из кода — игнорируется (FR-10.6)
                decision = await apply_verification(
                    self,
                    tx,
                    identity_id,
                    factor,
                    lambda flow, s, verified, now: flow.expire(s, now),
                    actor="timer",
                )
                changed += int(decision.changed)
        return changed


class RequirementsMixin(SpaceBase):
    """Что платформа требует сейчас (`ih_identities.requirements`), события requirement.added/resolved."""

    async def _add_requirement(
        self, identity_id: UUID, code: str, *, deadline: datetime | None = None, actor: str = "platform"
    ) -> bool:
        async with self._tx(own=True) as tx:
            await self._check_expected(tx)  # своя транзакция при using(session) — сверка здесь
            await tx.lock_identities([identity_id])  # список требований читается и пишется целиком
            row = await self._identity(tx, identity_id)
            if any(r.get("code") == code for r in row.requirements):
                return False
            now = await tx.now()
            item = {
                "code": code,
                "since": now.isoformat(),
                "deadline": deadline.isoformat() if deadline else None,
            }
            await tx.update_identity(identity_id, {"requirements": [*row.requirements, item]})
            await self._requirement_event(tx, row, "identity.requirement.added", item, actor)
            return True

    async def _resolve_requirement(
        self, identity_id: UUID, code: str, *, actor: str = "platform", reason: str | None = None
    ) -> bool:
        # Resolution is an operator command and must join a caller transaction
        # (the admin command gateway holds the identity revision lock there).
        async with self._tx() as tx:
            await tx.lock_identities([identity_id])
            row = await self._identity(tx, identity_id)
            rest = [r for r in row.requirements if r.get("code") != code]
            if len(rest) == len(row.requirements):
                return False
            await tx.update_identity(identity_id, {"requirements": rest})
            await self._requirement_event(
                tx,
                row,
                "identity.requirement.resolved",
                {"code": code, "reason": reason},
                actor,
            )
            return True

    async def _requirement_event(
        self, tx: StoreTx, row: IdentityRow, event: str, data: Mapping[str, Any], actor: str
    ) -> None:
        await self._emit(
            tx, EventDraft(event, actor=actor, service=row.service, identity_id=row.id, data=dict(data))
        )
