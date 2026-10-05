"""Операции провайдера: вход, обновление сессии и пробы как долговечная очередь (IMP-11, D-220).

Claim коммитится до вызова адаптера, результат применяется второй транзакцией — только при
совпадении claim и пока он не истёк. Гарантия — at-least-once попытка, а не exactly-once эффект:
после сбоя между вызовом и записью результата адаптер может быть вызван ещё раз (ключ
идемпотентности — `claim_id`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from identity_hub.errors import TransitionNotAllowed, ValidationFailed

__all__ = [
    "CANCELLED",
    "FAILED",
    "PENDING",
    "RETRYABLE",
    "RUNNING",
    "SUCCEEDED",
    "TERMINAL",
    "RetryPolicy",
    "next_state",
    "resolve_outcome",
    "resolve_recovery",
    "safe_error_code",
]

PENDING, RUNNING, RETRYABLE = "pending", "running", "retryable"
SUCCEEDED, FAILED, CANCELLED = "succeeded", "failed", "cancelled"
TERMINAL = frozenset({SUCCEEDED, FAILED, CANCELLED})
CLAIMABLE = frozenset({PENDING, RETRYABLE})

_TRANSITIONS: dict[tuple[str, str], str] = {
    (PENDING, "claim"): RUNNING,
    (RETRYABLE, "claim"): RUNNING,
    (RUNNING, "succeed"): SUCCEEDED,
    (RUNNING, "retry"): RETRYABLE,
    (RUNNING, "fail"): FAILED,
    (RUNNING, "recover"): RETRYABLE,  # claim истёк: воркер упал или завис
    (PENDING, "cancel"): CANCELLED,
    (RETRYABLE, "cancel"): CANCELLED,
}


def next_state(state: str, event: str) -> str:
    """Состояние после события; недопустимое — `TransitionNotAllowed`."""
    try:
        return _TRANSITIONS[(state, event)]
    except KeyError:
        raise TransitionNotAllowed("provider_operation", state, event) from None


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Повтор после безопасной временной ошибки: экспоненциальная задержка, предел попыток и срока."""

    max_attempts: int = 5
    base: timedelta = timedelta(seconds=5)
    max_delay: timedelta = timedelta(minutes=10)

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValidationFailed("max_attempts должен быть положительным")

    def after_failure(
        self, *, attempt: int, now: datetime, deadline_at: datetime | None
    ) -> tuple[str, datetime | None]:
        """(`retryable`, когда снова можно взять) или (`failed`, None): попытки исчерпаны или вышел срок."""
        if attempt >= self.max_attempts:
            return FAILED, None
        delay = min(self.base * 2 ** max(0, attempt - 1), self.max_delay)
        available_at = now + delay
        if deadline_at is not None and available_at >= deadline_at:
            return FAILED, None
        return RETRYABLE, available_at


def safe_error_code(error: BaseException) -> str:
    """Безопасный код ошибки адаптера: имя класса, без текста — в нём бывают секреты (D-220)."""
    return type(error).__name__[:64]


_OUTCOME_EVENTS = {SUCCEEDED: "succeed", RETRYABLE: "retry", FAILED: "fail"}


def resolve_outcome(
    outcome: str, *, attempt: int, deadline_at: datetime | None, now: datetime, policy: RetryPolicy | None
) -> tuple[str, datetime | None]:
    """Состояние по итогу адаптера; `retryable` по политике может стать `failed` (D-220)."""
    event = _OUTCOME_EVENTS.get(outcome)
    if event is None:
        raise ValidationFailed(f"итог операции: succeeded, retryable или failed, получено {outcome!r}")
    state = next_state(RUNNING, event)
    if state == RETRYABLE:
        return (policy or RetryPolicy()).after_failure(attempt=attempt, now=now, deadline_at=deadline_at)
    return state, None


def resolve_recovery(
    *, attempt: int, deadline_at: datetime | None, now: datetime, policy: RetryPolicy | None
) -> tuple[str, datetime | None]:
    """Истёкший claim: снова доступна по политике или `failed`, если попытки исчерпаны или вышел срок."""
    next_state(RUNNING, "recover")
    return (policy or RetryPolicy()).after_failure(attempt=attempt, now=now, deadline_at=deadline_at)
