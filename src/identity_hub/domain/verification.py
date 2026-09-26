"""Верификации: отдельная машина на каждый фактор (ТЗ §3.5, FR-10).

Состояния фиксированы библиотекой; набор факторов, зависимости, уровни, коды отказов
и сроки действия объявляет сервис через `VerificationSpec` и `VerificationTier`.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel

from identity_hub.domain.regions import (
    Command,
    Observation,
    RegionDecision,
    RegionSpec,
    RegionState,
    Timer,
    apply,
)
from identity_hub.enums import Severity, VerificationState
from identity_hub.errors import (
    AttemptsExhausted,
    TransitionNotAllowed,
    ValidationFailed,
    VerificationRequirementsNotMet,
)

__all__ = [
    "VERIFICATION",
    "VerificationDecision",
    "VerificationFlow",
    "VerificationMode",
    "VerificationSpec",
    "VerificationStatus",
    "VerificationTier",
    "achieved_tiers",
    "effective_factors",
    "required_factors",
    "verified_factors_after",
]

V = VerificationState


class VerificationMode(StrEnum):
    AUTO = "auto"  # платформа проверяет сразу: submit → verified
    MANUAL = "manual"  # submit → under_review → approve / reject


@dataclass(frozen=True)
class VerificationSpec:
    """Декларация фактора верификации сервисом (FR-10.1)."""

    name: str
    requires: frozenset[str] = frozenset()
    mode: VerificationMode = VerificationMode.MANUAL
    valid_for: timedelta | None = None
    max_attempts: int | None = None
    reject_codes: frozenset[str] = frozenset()
    evidence: type[BaseModel] | None = None
    pending_timeout: timedelta | None = None  # pending → missing по истечении

    def __post_init__(self) -> None:
        if self.name in self.requires:
            raise ValueError(f"верификация {self.name} зависит от самой себя")
        if self.max_attempts is not None and self.max_attempts < 1:
            raise ValueError("max_attempts должен быть ≥ 1")


@dataclass(frozen=True)
class VerificationTier:
    """Уровень верификации — вычисляется из факторов, не хранится (FR-10.2)."""

    name: str
    factors: frozenset[str]


def _normalize(current: str, observed: str) -> str:
    # платформа больше не видит подтверждения → оно отозвано (FR-9.5)
    if current == V.VERIFIED and observed == V.MISSING:
        return V.REVOKED
    return observed


def _fs(*values: str) -> frozenset[str]:
    return frozenset(values)


VERIFICATION = RegionSpec(
    name="verification",
    states=_fs(*V),
    initial=V.MISSING,
    event_type="identity.verification.changed",
    transitions={
        V.MISSING: _fs(V.PENDING),
        V.PENDING: _fs(V.MISSING, V.UNDER_REVIEW, V.VERIFIED),
        V.UNDER_REVIEW: _fs(V.VERIFIED, V.REJECTED),
        V.REJECTED: _fs(V.PENDING),
        V.VERIFIED: _fs(V.EXPIRED, V.REVOKED),
        V.EXPIRED: _fs(V.PENDING),
        V.REVOKED: _fs(V.PENDING),
    },
    severity={V.REJECTED: Severity.WARNING, V.REVOKED: Severity.WARNING, V.EXPIRED: Severity.WARNING},
    on_expire={V.VERIFIED: V.EXPIRED, V.PENDING: V.MISSING},
    normalize=_normalize,
)


@dataclass(frozen=True, slots=True)
class VerificationStatus:
    """Состояние одного фактора: регион + данные ih_verifications."""

    region: RegionState
    attempts: int = 0
    started_at: datetime | None = None
    submitted_at: datetime | None = None
    verified_at: datetime | None = None

    @property
    def state(self) -> str:
        return self.region.state

    @property
    def expires_at(self) -> datetime | None:
        return self.region.until if self.region.state == V.VERIFIED else None


@dataclass(frozen=True, slots=True)
class VerificationDecision:
    previous: VerificationStatus
    status: VerificationStatus
    region: RegionDecision | None  # None — возобновление без изменения региона

    @property
    def changed(self) -> bool:
        return self.status != self.previous


class VerificationFlow:
    """Правила одного фактора поверх региона `VERIFICATION`."""

    def __init__(self, spec: VerificationSpec) -> None:
        self.spec = spec

    def initial(self, now: datetime) -> VerificationStatus:
        return VerificationStatus(region=VERIFICATION.initial_state(now))

    # --------------------------------------------------------- команды

    def start(
        self, status: VerificationStatus, verified_factors: Collection[str], now: datetime, actor: str
    ) -> VerificationDecision:
        """Начать или возобновить (FR-10.3): проверка зависимостей и лимита попыток."""
        if status.state == V.PENDING:
            return VerificationDecision(status, status, None)  # возобновление, попытка не тратится
        missing = sorted(self.spec.requires - set(verified_factors))
        if missing:
            raise VerificationRequirementsNotMet(self.spec.name, missing)
        if self.spec.max_attempts is not None and status.attempts >= self.spec.max_attempts:
            raise AttemptsExhausted(self.spec.name, status.attempts)
        until = now + self.spec.pending_timeout if self.spec.pending_timeout else None
        region = self._command(status, Command(V.PENDING, actor=actor, until=until), now)
        return self._decided(status, region, started_at=now)

    def submit(self, status: VerificationStatus, now: datetime, actor: str) -> VerificationDecision:
        """Данные отправлены платформе; попытка засчитывается здесь (FR-10.3)."""
        if status.state != V.PENDING:
            raise TransitionNotAllowed(
                VERIFICATION.name, status.state, V.UNDER_REVIEW, "submit только из pending"
            )
        attempts = status.attempts + 1
        if self.spec.mode is VerificationMode.AUTO:
            region = self._command(
                status, Command(V.VERIFIED, actor=actor, until=self._valid_until(now)), now
            )
            return self._decided(status, region, attempts=attempts, submitted_at=now, verified_at=now)
        region = self._command(status, Command(V.UNDER_REVIEW, actor=actor), now)
        return self._decided(status, region, attempts=attempts, submitted_at=now)

    def approve(self, status: VerificationStatus, now: datetime, actor: str) -> VerificationDecision:
        region = self._command(status, Command(V.VERIFIED, actor=actor, until=self._valid_until(now)), now)
        return self._decided(status, region, verified_at=now)

    def reject(
        self, status: VerificationStatus, code: str, now: datetime, actor: str
    ) -> VerificationDecision:
        if self.spec.reject_codes and code not in self.spec.reject_codes:
            raise ValidationFailed(f"верификация {self.spec.name}: неизвестный код отказа {code!r}")
        region = self._command(status, Command(V.REJECTED, actor=actor, code=code), now)
        return self._decided(status, region)

    def abandon(self, status: VerificationStatus, now: datetime, actor: str) -> VerificationDecision:
        region = self._command(status, Command(V.MISSING, actor=actor), now)
        return self._decided(status, region)

    def revoke(
        self, status: VerificationStatus, now: datetime, actor: str, code: str | None = None
    ) -> VerificationDecision:
        region = self._command(status, Command(V.REVOKED, actor=actor, code=code), now)
        return self._decided(status, region)

    # --------------------------------------------------------- наблюдения и таймер

    def reconcile(
        self, status: VerificationStatus, observation: Observation, now: datetime
    ) -> VerificationDecision:
        """Применить наблюдение платформы (FR-9.1, 9.5). Срок действия ставится при переходе в verified."""
        if observation.state == V.VERIFIED and observation.until is None and status.state != V.VERIFIED:
            observation = replace(observation, until=self._valid_until(observation.observed_at))
        region = apply(VERIFICATION, status.region, observation, now, key=self.spec.name)
        verified_at = now if region.transitioned and region.state.state == V.VERIFIED else None
        return self._decided(status, region, verified_at=verified_at)

    def expire(self, status: VerificationStatus, now: datetime) -> VerificationDecision:
        region = apply(VERIFICATION, status.region, Timer(), now, key=self.spec.name)
        return self._decided(status, region)

    # --------------------------------------------------------- вспомогательное

    def _valid_until(self, now: datetime) -> datetime | None:
        return now + self.spec.valid_for if self.spec.valid_for else None

    def _command(self, status: VerificationStatus, cmd: Command, now: datetime) -> RegionDecision:
        return apply(VERIFICATION, status.region, cmd, now, key=self.spec.name)

    @staticmethod
    def _decided(
        status: VerificationStatus,
        region: RegionDecision,
        *,
        attempts: int | None = None,
        started_at: datetime | None = None,
        submitted_at: datetime | None = None,
        verified_at: datetime | None = None,
    ) -> VerificationDecision:
        new = replace(
            status,
            region=region.state,
            attempts=status.attempts if attempts is None else attempts,
            started_at=started_at or status.started_at,
            submitted_at=submitted_at or status.submitted_at,
            verified_at=verified_at or status.verified_at,
        )
        return VerificationDecision(status, new, region)


# ---------------------------------------------------------------- факторы и уровни


def effective_factors(verified: Iterable[str], known: Collection[str]) -> frozenset[str]:
    """Факторы, удалённые из кода сервиса, игнорируются правилами (FR-10.6)."""
    return frozenset(f for f in verified if f in known)


def achieved_tiers(verified: Iterable[str], tiers: Iterable[VerificationTier]) -> frozenset[str]:
    """Уровни, все факторы которых подтверждены (FR-10.2)."""
    have = frozenset(verified)
    return frozenset(t.name for t in tiers if t.factors <= have)


def required_factors(
    factors: Iterable[str], tier_names: Iterable[str], tiers: Mapping[str, VerificationTier]
) -> frozenset[str]:
    """Объединение факторов правила и факторов уровней (ТЗ §3.9: rule.verified ∪ tier)."""
    result = set(factors)
    for name in tier_names:
        if name not in tiers:
            raise ValidationFailed(f"неизвестный уровень верификации {name!r}")
        result |= tiers[name].factors
    return frozenset(result)


def verified_factors_after(current: Iterable[str], factor: str, state: str) -> frozenset[str]:
    """Новое значение ih_identities.verified_factors после перехода фактора (FR-10.5)."""
    rest = frozenset(f for f in current if f != factor)
    return rest | {factor} if state == V.VERIFIED else rest
