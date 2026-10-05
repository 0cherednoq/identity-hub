"""IMP-11 (I4.1): машина состояний операции провайдера и политика повторов (D-220)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from identity_hub.domain.provider_operations import (
    CANCELLED,
    FAILED,
    PENDING,
    RETRYABLE,
    RUNNING,
    SUCCEEDED,
    TERMINAL,
    RetryPolicy,
    next_state,
    safe_error_code,
)
from identity_hub.errors import TransitionNotAllowed

pytestmark = pytest.mark.unit

NOW = datetime(2026, 1, 1, tzinfo=UTC)

ALLOWED = {
    (PENDING, "claim"): RUNNING,
    (RETRYABLE, "claim"): RUNNING,
    (RUNNING, "succeed"): SUCCEEDED,
    (RUNNING, "retry"): RETRYABLE,
    (RUNNING, "fail"): FAILED,
    (RUNNING, "recover"): RETRYABLE,
    (PENDING, "cancel"): CANCELLED,
    (RETRYABLE, "cancel"): CANCELLED,
}
STATES = (PENDING, RUNNING, RETRYABLE, SUCCEEDED, FAILED, CANCELLED)
EVENTS = ("claim", "succeed", "retry", "fail", "recover", "cancel")


@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize("event", EVENTS)
def test_state_table(state: str, event: str) -> None:
    expected = ALLOWED.get((state, event))
    if expected is None:
        with pytest.raises(TransitionNotAllowed):
            next_state(state, event)
    else:
        assert next_state(state, event) == expected


def test_terminal_states() -> None:
    assert frozenset({SUCCEEDED, FAILED, CANCELLED}) == TERMINAL


def test_retry_policy_backoff_attempts_and_deadline() -> None:
    policy = RetryPolicy(max_attempts=3, base=timedelta(seconds=5), max_delay=timedelta(seconds=12))

    assert policy.after_failure(attempt=1, now=NOW, deadline_at=None) == (
        RETRYABLE,
        NOW + timedelta(seconds=5),
    )
    assert policy.after_failure(attempt=2, now=NOW, deadline_at=None) == (
        RETRYABLE,
        NOW + timedelta(seconds=10),
    )
    assert policy.after_failure(attempt=3, now=NOW, deadline_at=None) == (FAILED, None)  # попытки исчерпаны
    late = policy.after_failure(attempt=1, now=NOW, deadline_at=NOW + timedelta(seconds=3))
    assert late == (FAILED, None)  # следующая попытка была бы после срока
    capped = RetryPolicy(max_attempts=10, base=timedelta(seconds=5), max_delay=timedelta(seconds=12))
    assert capped.after_failure(attempt=4, now=NOW, deadline_at=None) == (
        RETRYABLE,
        NOW + timedelta(seconds=12),
    )


def test_safe_error_code_keeps_only_class_name() -> None:
    class PlatformRejected(Exception):
        pass

    assert safe_error_code(PlatformRejected("password=secret token=x")) == "PlatformRejected"
    assert len(safe_error_code(type("E" * 100, (Exception,), {})())) == 64
