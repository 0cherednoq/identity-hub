"""Исключения Identity Hub.

Все наследуются от `IdentityHubError`. Сообщения не содержат секретов и `token` аренды (ТЗ S-8).
У каждого типа есть стабильный `code` и безопасный `context` (IMP-13): только идентификаторы,
имена и счётчики — без секретов, SQL-параметров и сырых ответов платформ. Отображение в HTTP
и код выхода CLI — чистые функции `http_status()` и `exit_code()` без зависимости от веб-фреймворка.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, ClassVar
from uuid import UUID

__all__ = [
    "AlreadyExists",
    "AttemptsExhausted",
    "Blocker",
    "ChallengeAlreadyOpen",
    "ConcurrentModification",
    "DatabaseBusy",
    "IdempotencyConflict",
    "IdentityHubError",
    "LeaseExpired",
    "LeaseLost",
    "LeaseOwnershipLost",
    "LeaseRevoked",
    "LeaseUnavailable",
    "NotFound",
    "OperationNotAllowed",
    "QuotaExhausted",
    "RevisionConflict",
    "SchemaIncompatible",
    "SecretConfigurationError",
    "SpaceMismatch",
    "StaleFencing",
    "StorageError",
    "TransitionNotAllowed",
    "ValidationFailed",
    "VerificationRequirementsNotMet",
    "describe_error",
    "exit_code",
    "http_status",
]


def _safe(value: Any) -> Any:
    """Значение для `context`: JSON-совместимое, без произвольных объектов."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return _safe(value.value)
    if isinstance(value, Mapping):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, Sequence | set | frozenset):
        return [_safe(v) for v in value]
    return type(value).__name__  # неизвестный объект не сериализуется целиком


class IdentityHubError(Exception):
    """Базовое исключение библиотеки."""

    code: ClassVar[str] = "internal_error"
    # атрибуты экземпляра, которые безопасно отдать наружу в `context`
    context_fields: ClassVar[tuple[str, ...]] = ()

    @property
    def context(self) -> dict[str, Any]:
        """Безопасные подробности для API и логов: идентификаторы, имена, счётчики."""
        return {name: _safe(getattr(self, name, None)) for name in self.context_fields}

    def __reduce__(self) -> tuple[Any, ...]:
        # исключения с именованными полями должны переживать pickle (multiprocessing, брокеры)
        return (_rebuild, (type(self), self.__dict__, self.args))


def _rebuild(cls: type[IdentityHubError], state: dict[str, Any], args: tuple[Any, ...]) -> IdentityHubError:
    exc = cls.__new__(cls)
    Exception.__init__(exc, *args)
    exc.__dict__.update(state)
    return exc


class NotFound(IdentityHubError):
    code = "not_found"
    context_fields = ("entity", "key")

    def __init__(self, entity: str, key: object) -> None:
        self.entity = entity
        self.key = key
        super().__init__(f"{entity} не найден: {key}")


class AlreadyExists(IdentityHubError):
    """Повтор идемпотентной операции: объект уже есть, изменений не сделано."""

    code = "already_exists"
    context_fields = ("entity", "key", "existing_id")

    def __init__(self, entity: str, key: object, existing_id: UUID | None = None) -> None:
        self.entity = entity
        self.key = key
        self.existing_id = existing_id
        super().__init__(f"{entity} уже существует: {key}")


class SpaceMismatch(IdentityHubError):
    """Объект из другого пространства или смена пространства внутри связанной сессии (ТЗ T-3)."""

    code = "space_mismatch"


class ValidationFailed(IdentityHubError):
    code = "validation_error"
    context_fields = ("errors",)

    def __init__(self, message: str, errors: Sequence[Any] = ()) -> None:
        self.errors = list(errors)
        super().__init__(message)


class TransitionNotAllowed(IdentityHubError):
    code = "invalid_transition"
    context_fields = ("region", "from_state", "to_state", "reason")

    def __init__(self, region: str, from_state: str, to_state: str, reason: str | None = None) -> None:
        self.region = region
        self.from_state = from_state
        self.to_state = to_state
        self.reason = reason
        msg = f"переход {region}: {from_state} → {to_state} недопустим"
        super().__init__(f"{msg} ({reason})" if reason else msg)


class ConcurrentModification(IdentityHubError):
    """Оптимистическая блокировка: запись изменена параллельно."""

    code = "concurrent_modification"
    context_fields = ("entity", "key", "expected_version", "actual_version")

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


class RevisionConflict(ConcurrentModification):
    """`expected_revision` не совпал с текущей ревизией агрегата (IMP-07)."""

    code = "revision_conflict"


class IdempotencyConflict(IdentityHubError):
    """Тот же ключ идемпотентности пришёл с другой командой (IMP-03). Ключ в `context` не попадает."""

    code = "idempotency_conflict"
    context_fields = ("operation",)

    def __init__(self, operation: str) -> None:
        self.operation = operation
        super().__init__(f"ключ идемпотентности уже использован другой командой {operation}")


@dataclass(frozen=True, slots=True)
class Blocker:
    """Причина, по которой операция недопустима (ТЗ §3.9)."""

    code: str  # lifecycle | standing | auth | availability | verification | quota | proxy
    detail: str
    until: datetime | None = None


class OperationNotAllowed(IdentityHubError):
    code = "operation_not_allowed"
    context_fields = ("kind", "blocker_codes")

    def __init__(self, kind: str, blockers: Sequence[Blocker]) -> None:
        self.kind = kind
        self.blockers = list(blockers)
        reasons = "; ".join(f"{b.code}: {b.detail}" for b in self.blockers)
        super().__init__(f"операция {kind} недопустима: {reasons}")

    @property
    def blocker_codes(self) -> list[str]:
        return [b.code for b in self.blockers]


class QuotaExhausted(IdentityHubError):
    code = "quota_exceeded"
    context_fields = ("kind", "window_seconds", "retry_at")

    def __init__(self, kind: str, window_seconds: int, retry_at: datetime | None = None) -> None:
        self.kind = kind
        self.window_seconds = window_seconds
        self.retry_at = retry_at
        super().__init__(f"квота {kind} за {window_seconds} с исчерпана до {retry_at}")


class LeaseUnavailable(IdentityHubError):
    """Нет свободного аккаунта под условия аренды (с учётом wait)."""

    code = "no_resource_available"


class LeaseLost(IdentityHubError):
    """Аренда потеряна: воркер обязан прекратить работу с аккаунтом (ТЗ FR-6.3)."""

    code = "lease_lost"
    context_fields = ("lease_id", "reason")

    def __init__(self, lease_id: UUID, reason: str) -> None:
        self.lease_id = lease_id
        self.reason = reason
        super().__init__(f"аренда {lease_id} потеряна: {reason}")


class LeaseOwnershipLost(LeaseLost):
    """Аренду держит другой владелец: fence не совпал (IMP-05)."""

    code = "lease_ownership_lost"


class LeaseRevoked(LeaseLost):
    """Аренда отозвана администратором (IMP-06)."""

    code = "lease_revoked"


class LeaseExpired(LeaseLost):
    """Срок аренды истёк без heartbeat (IMP-06)."""

    code = "lease_expired"


class StaleFencing(IdentityHubError):
    """Запись от имени устаревшей аренды отклонена (ТЗ G-1.4)."""

    code = "stale_fencing"
    context_fields = ("identity_id", "fencing", "current")

    def __init__(self, identity_id: UUID, fencing: int, current: int | None = None) -> None:
        self.identity_id = identity_id
        self.fencing = fencing
        self.current = current
        super().__init__(f"устаревший fencing {fencing} для {identity_id} (текущий {current})")


class ChallengeAlreadyOpen(IdentityHubError):
    code = "challenge_already_open"
    context_fields = ("identity_id",)

    def __init__(self, identity_id: UUID) -> None:
        self.identity_id = identity_id
        super().__init__(f"у аккаунта {identity_id} уже открыт challenge")


class VerificationRequirementsNotMet(IdentityHubError):
    code = "verification_requirements_not_met"
    context_fields = ("factor", "missing")

    def __init__(self, factor: str, missing: Sequence[str]) -> None:
        self.factor = factor
        self.missing = list(missing)
        super().__init__(f"верификация {factor} требует: {', '.join(self.missing)}")


class AttemptsExhausted(IdentityHubError):
    code = "attempts_exhausted"
    context_fields = ("factor", "attempts")

    def __init__(self, factor: str, attempts: int) -> None:
        self.factor = factor
        self.attempts = attempts
        super().__init__(f"верификация {factor}: исчерпаны попытки ({attempts})")


class DatabaseBusy(IdentityHubError):
    """БД не уложилась в отведённое время: lock/statement timeout, deadlock, конфликт сериализации.

    Повторить позже. SQL, параметры запроса и исходное исключение драйвера не сохраняются.
    """

    code = "database_busy"
    context_fields = ("sqlstate",)

    def __init__(self, message: str | None = None, *, sqlstate: str | None = None) -> None:
        self.sqlstate = sqlstate
        super().__init__(message or "база данных занята: блокировка или запрос не уложились в срок")


class StorageError(IdentityHubError):
    """Ошибка хранилища, не описанная другим типом: нарушение ограничения, потеря соединения.

    В сообщении только SQLSTATE: SQL, параметры (среди них `token` аренды и шифртексты) и исходное
    исключение драйвера наружу не выходят (S-8).
    """

    code = "storage_error"
    context_fields = ("sqlstate",)

    def __init__(self, sqlstate: str | None = None) -> None:
        self.sqlstate = sqlstate
        super().__init__(f"ошибка хранилища (SQLSTATE {sqlstate or 'неизвестен'})")


class SchemaIncompatible(IdentityHubError):
    """Ревизия схемы БД не та, что ожидает библиотека (IMP-15). URL и SQL в сообщение не попадают."""

    code = "schema_incompatible"
    context_fields = ("expected", "actual")

    def __init__(self, expected: str, actual: str | None) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(f"схема БД {actual or 'отсутствует'}, ожидается {expected}")


class SecretConfigurationError(IdentityHubError):
    """Ключи шифрования не настроены или не подходят. Материал ключей в сообщение не попадает."""

    code = "secret_configuration_error"


# ------------------------------------------------------------------ отображение кодов

_HTTP_STATUS: dict[str, int] = {
    "validation_error": 422,
    "not_found": 404,
    "no_resource_available": 404,
    "already_exists": 409,
    "space_mismatch": 409,
    "invalid_transition": 409,
    "concurrent_modification": 409,
    "revision_conflict": 409,
    "idempotency_conflict": 409,
    "operation_not_allowed": 409,
    "lease_lost": 409,
    "lease_ownership_lost": 409,
    "lease_revoked": 409,
    "lease_expired": 409,
    "stale_fencing": 409,
    "challenge_already_open": 409,
    "verification_requirements_not_met": 409,
    "attempts_exhausted": 409,
    "quota_exceeded": 429,
    "database_busy": 503,
    "storage_error": 500,
    "schema_incompatible": 503,
    "secret_configuration_error": 500,
    "decryption_failed": 500,
    "internal_error": 500,
}

_EXIT_CODE: dict[str, int] = {
    "validation_error": 2,
    "not_found": 2,
    "schema_incompatible": 3,
    "database_busy": 4,
    "secret_configuration_error": 5,
    "decryption_failed": 5,
}


def describe_error(exc: BaseException) -> str:
    """Безопасное описание исключения для логов и отчётов: имя типа и стабильный код.

    Текст чужого исключения не используется: в нём бывают URL с ключами, параметры SQL и `token`.
    """
    if isinstance(exc, IdentityHubError):
        return f"{type(exc).__name__}[{exc.code}]"
    return type(exc).__name__


def http_status(error: IdentityHubError) -> int:
    """HTTP-статус для ошибки библиотеки (IMP-13); неизвестный код — 500."""
    return _HTTP_STATUS.get(error.code, 500)


def exit_code(error: IdentityHubError) -> int:
    """Код выхода CLI для ошибки библиотеки (IMP-13); прочие ошибки — 1."""
    return _EXIT_CODE.get(error.code, 1)
