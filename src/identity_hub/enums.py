"""Перечисления состояний Identity Hub.

Хранятся в БД как String + CHECK (не ENUM Postgres), см. ТЗ §8.4.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "AuthState",
    "Availability",
    "ChangeSource",
    "DeliveryStatus",
    "LeaseEndReason",
    "Lifecycle",
    "ProxyStatus",
    "SessionStatus",
    "Severity",
    "Standing",
    "VerificationState",
]


class Lifecycle(StrEnum):
    """Решения владельца. Не синхронизируется: наблюдать нечего."""

    PROVISIONING = "provisioning"  # регистрируется / импортирован и не проверен
    READY = "ready"  # в парке
    RETIRED = "retired"  # выведен


class Standing(StrEnum):
    """Положение у платформы. Только наблюдения, команды и ручные правки."""

    GOOD = "good"
    LIMITED = "limited"  # временные ограничения: spam restriction, action block
    FROZEN = "frozen"
    APPEALING = "appealing"
    BANNED = "banned"
    DELETED = "deleted"


class AuthState(StrEnum):
    NO_SESSION = "no_session"
    LOGGING_IN = "logging_in"
    CHALLENGE = "challenge"  # платформа ждёт ответа человека
    AUTHENTICATED = "authenticated"
    SESSION_INVALID = "session_invalid"


class Availability(StrEnum):
    """Внутреннее планирование библиотеки. Не синхронизируется."""

    AVAILABLE = "available"
    PAUSED = "paused"  # pacing
    COOLING = "cooling"  # rate limit / flood-wait
    QUARANTINED = "quarantined"  # error_score превысил порог


class ChangeSource(StrEnum):
    COMMAND = "command"  # наше намерение, прошло guard-ы
    PROBE = "probe"  # мы спросили платформу
    ERROR = "error"  # классификатор увидел ошибку во время операции
    WEBHOOK = "webhook"  # платформа сообщила сама
    OVERRIDE = "override"  # ручная правка человеком
    TIMER = "timer"  # истёк срок
    IMPORT = "import"


class SessionStatus(StrEnum):
    """Жизненный цикл записи сессии (IMP-10, D-218): `created → active → invalidated → revoked`."""

    CREATED = "created"
    ACTIVE = "active"
    INVALIDATED = "invalidated"
    REVOKED = "revoked"  # конечное: запись остаётся в истории слота
    VALID = "active"  # псевдоним ACTIVE для кода до 0023
    INVALID = "invalidated"  # псевдоним INVALIDATED


class VerificationState(StrEnum):
    MISSING = "missing"
    PENDING = "pending"  # процесс начат: код отправлен, форма открыта
    UNDER_REVIEW = "under_review"  # данные отправлены, ждём решения платформы
    VERIFIED = "verified"
    REJECTED = "rejected"
    EXPIRED = "expired"
    REVOKED = "revoked"  # платформа сбросила подтверждение


class ProxyStatus(StrEnum):
    ALIVE = "alive"
    QUARANTINED = "quarantined"
    DEAD = "dead"


class LeaseEndReason(StrEnum):
    RELEASED = "released"  # воркер вышел из блока lease() штатно
    FAILED = "failed"  # вышел с исключением
    LOST = "lost"  # воркер сам признал аренду потерянной (heartbeat не прошёл)
    EXPIRED = "expired"  # TTL истёк, аренду перезаписал следующий арендатор или чистильщик
    REVOKED = "revoked"  # отозвана из админки


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"  # попытка неудачна, будет повтор
    DEAD = "dead"  # попытки исчерпаны
    PAUSED = "paused"  # endpoint отключён: доставка ждёт его включения (только вебхуки)
