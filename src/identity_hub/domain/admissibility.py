"""Вычисляемая допустимость операции (ТЗ §3.9).

Допустимость не хранится: она выводится из регионов, подтверждённых факторов и квот.
`evaluate()` возвращает список причин отказа; `RuleFilter` — то же правило в форме,
которую хранилище компилирует в условие выборки аренды (P3.1).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from identity_hub.domain.verification import effective_factors, required_factors
from identity_hub.enums import AuthState, Availability, Lifecycle
from identity_hub.errors import Blocker
from identity_hub.service import Quota, Service

__all__ = ["IdentitySnapshot", "QuotaUsage", "RuleFilter", "compile_rule", "evaluate"]


@dataclass(frozen=True, slots=True)
class IdentitySnapshot:
    """Денормализованные регионы аккаунта с ih_identities."""

    lifecycle: str
    standing: str
    auth_state: str
    availability: str
    availability_until: datetime | None = None
    standing_code: str | None = None
    verified_factors: frozenset[str] = frozenset()
    # подтверждённые факторы, чей срок действия уже вышел, а обслуживание ещё не прошло
    expired_factors: frozenset[str] = frozenset()

    def is_available(self, now: datetime) -> bool:
        """Временные состояния снимаются лениво: истёкший срок = доступен (ТЗ §3.4)."""
        if self.availability == Availability.AVAILABLE:
            return True
        return (
            self.availability in (Availability.PAUSED, Availability.COOLING)
            and self.availability_until is not None
            and self.availability_until <= now
        )


@dataclass(frozen=True, slots=True)
class QuotaUsage:
    """Использование одного окна квоты: ключ — (вид, длина окна)."""

    used: int
    window_start: datetime


@dataclass(frozen=True, slots=True)
class RuleFilter:
    """Правило операции в форме фильтра выборки (без квот — они списываются атомарно при операции)."""

    standing: frozenset[str]
    needs_auth: bool
    required_factors: frozenset[str]


def compile_rule(service: Service[Any, Any], kind: str) -> RuleFilter:
    rule = service.rule(kind)
    factors = required_factors(rule.verified, rule.tiers, service.tiers_by_name)
    return RuleFilter(standing=rule.standing, needs_auth=rule.needs_auth, required_factors=factors)


def evaluate(
    service: Service,
    kind: str,
    identity: IdentitySnapshot,
    now: datetime,
    quota_usage: Mapping[tuple[str, int], QuotaUsage] | None = None,
) -> list[Blocker]:
    """Причины, по которым операция `kind` недопустима; пустой список — допустима."""
    rule = service.rule(kind)
    flt = compile_rule(service, kind)
    blockers: list[Blocker] = []

    if identity.lifecycle != Lifecycle.READY:
        blockers.append(Blocker("lifecycle", identity.lifecycle))
    if identity.standing not in flt.standing:
        detail = (
            identity.standing
            if identity.standing_code is None
            else f"{identity.standing}:{identity.standing_code}"
        )
        blockers.append(Blocker("standing", detail))
    if flt.needs_auth and identity.auth_state != AuthState.AUTHENTICATED:
        blockers.append(Blocker("auth", identity.auth_state))
    if not identity.is_available(now):
        blockers.append(Blocker("availability", identity.availability, until=identity.availability_until))
    # срок действия проверяется сразу, а не после прогона обслуживания (R2-F13)
    have = effective_factors(identity.verified_factors, service.factor_names) - identity.expired_factors
    missing = flt.required_factors - have
    if missing:
        blockers.append(Blocker("verification", ",".join(sorted(missing))))
    blockers.extend(_quota_blockers(kind, rule.quotas, quota_usage or {}, now))
    return blockers


def _quota_blockers(
    kind: str,
    quotas: Collection[Quota],
    usage: Mapping[tuple[str, int], QuotaUsage],
    now: datetime,
) -> list[Blocker]:
    blockers: list[Blocker] = []
    for quota in quotas:
        current = usage.get((kind, quota.window_seconds))
        start = quota.window_start(now)
        if current is None or current.window_start != start:
            continue  # окно ещё не начато или сменилось — счётчик ноль
        if current.used >= quota.limit:
            blockers.append(Blocker("quota", f"{kind}/{quota.window_seconds}s", until=start + quota.per))
    return blockers
