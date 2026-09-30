"""Исключения Identity Hub.

Все наследуются от `IdentityHubError`. Сообщения не содержат секретов и `token` аренды (ТЗ S-8).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

__all__ = [
    "AlreadyExists",
    "AttemptsExhausted",
    "Blocker",
    "ChallengeAlreadyOpen",
    "ConcurrentModification",
    "IdentityHubError",
    "LeaseLost",
    "LeaseUnavailable",
    "NotFound",
    "OperationNotAllowed",
    "QuotaExhausted",
    "SpaceMismatch",
    "StaleFencing",
    "TransitionNotAllowed",
    "ValidationFailed",
    "VerificationRequirementsNotMet",
]


class IdentityHubError(Exception):
    """Базовое исключение библиотеки."""

    def __reduce__(self) -> tuple[Any, ...]:
        # исключения с именованными полями должны переживать pickle (multiprocessing, брокеры)
        return (_rebuild, (type(self), self.__dict__, self.args))


def _rebuild(cls: type[IdentityHubError], state: dict[str, Any], args: tuple[Any, ...]) -> IdentityHubError:
    exc = cls.__new__(cls)
    Exception.__init__(exc, *args)
    exc.__dict__.update(state)
    return exc


class NotFound(IdentityHubError):
    def __init__(self, entity: str, key: object) -> None:
        self.entity = entity
        self.key = key
        super().__init__(f"{entity} не найден: {key}")


class AlreadyExists(IdentityHubError):
    """Повтор идемпотентной операции: объект уже есть, изменений не сделано."""

    def __init__(self, entity: str, key: object, existing_id: UUID | None = None) -> None:
        self.entity = entity
        self.key = key
        self.existing_id = existing_id
        super().__init__(f"{entity} уже существует: {key}")


class SpaceMismatch(IdentityHubError):
    """Объект из другого пространства или смена пространства внутри связанной сессии (ТЗ T-3)."""


class ValidationFailed(IdentityHubError):
    def __init__(self, message: str, errors: Sequence[Any] = ()) -> None:
        self.errors = list(errors)
        super().__init__(message)


class TransitionNotAllowed(IdentityHubError):
    def __init__(self, region: str, from_state: str, to_state: str, reason: str | None = None) -> None:
        self.region = region
        self.from_state = from_state
        self.to_state = to_state
        self.reason = reason
        msg = f"переход {region}: {from_state} → {to_state} недопустим"
        super().__init__(f"{msg} ({reason})" if reason else msg)


class ConcurrentModification(IdentityHubError):
    """Оптимистическая блокировка: запись изменена параллельно."""

    def __init__(
        self,
        entity: str,
        key: object,
        expected_version: int | None = None,
        actual_version: int | None = None,
    ) -> None:
        self.entity = entity
        self.key = key
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(f"{entity} {key} изменён параллельно (ожидалась версия {expected_version})")


@dataclass(frozen=True, slots=True)
class Blocker:
    """Причина, по которой операция недопустима (ТЗ §3.9)."""

    code: str  # lifecycle | standing | auth | availability | verification | quota | proxy
    detail: str
    until: datetime | None = None


class OperationNotAllowed(IdentityHubError):
    def __init__(self, kind: str, blockers: Sequence[Blocker]) -> None:
        self.kind = kind
        self.blockers = list(blockers)
        reasons = "; ".join(f"{b.code}: {b.detail}" for b in self.blockers)
        super().__init__(f"операция {kind} недопустима: {reasons}")


class QuotaExhausted(IdentityHubError):
    def __init__(self, kind: str, window_seconds: int, retry_at: datetime | None = None) -> None:
        self.kind = kind
        self.window_seconds = window_seconds
        self.retry_at = retry_at
        super().__init__(f"квота {kind} за {window_seconds} с исчерпана до {retry_at}")


class LeaseUnavailable(IdentityHubError):
    """Нет свободного аккаунта под условия аренды (с учётом wait)."""


class LeaseLost(IdentityHubError):
    """Аренда потеряна: воркер обязан прекратить работу с аккаунтом (ТЗ FR-6.3)."""

    def __init__(self, lease_id: UUID, reason: str) -> None:
        self.lease_id = lease_id
        self.reason = reason
        super().__init__(f"аренда {lease_id} потеряна: {reason}")


class StaleFencing(IdentityHubError):
    """Запись от имени устаревшей аренды отклонена (ТЗ G-1.4)."""

    def __init__(self, identity_id: UUID, fencing: int, current: int | None = None) -> None:
        self.identity_id = identity_id
        self.fencing = fencing
        self.current = current
        super().__init__(f"устаревший fencing {fencing} для {identity_id} (текущий {current})")


class ChallengeAlreadyOpen(IdentityHubError):
    def __init__(self, identity_id: UUID) -> None:
        self.identity_id = identity_id
        super().__init__(f"у аккаунта {identity_id} уже открыт challenge")


class VerificationRequirementsNotMet(IdentityHubError):
    def __init__(self, factor: str, missing: Sequence[str]) -> None:
        self.factor = factor
        self.missing = list(missing)
        super().__init__(f"верификация {factor} требует: {', '.join(self.missing)}")


class AttemptsExhausted(IdentityHubError):
    def __init__(self, factor: str, attempts: int) -> None:
        self.factor = factor
        self.attempts = attempts
        super().__init__(f"верификация {factor}: исчерпаны попытки ({attempts})")
