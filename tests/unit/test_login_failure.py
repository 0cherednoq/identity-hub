"""Контракт структурированного отказа входа и срока Fault (server-admins S1/S2)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from identity_hub import Blame, Fault, LoginFailure, LoginFailureCode

pytestmark = pytest.mark.unit


def test_login_failure_has_safe_validated_code() -> None:
    failure = LoginFailure(LoginFailureCode.CREDENTIALS_REJECTED, retryable=False)
    assert (failure.code, failure.retryable, str(failure)) == (
        "credentials_rejected",
        False,
        "credentials_rejected",
    )
    for invalid in ("", "UPPER", "1bad", "contains space", "a" * 65):
        with pytest.raises(ValueError):
            LoginFailure(invalid)


def test_fault_until_accepts_only_expiring_standing() -> None:
    until = datetime(2026, 1, 2, tzinfo=UTC)
    assert Fault(Blame.IDENTITY_BANNED, standing="limited", until=until).until == until
    assert Fault(Blame.IDENTITY_BANNED, until=until).until == until  # временный banned
    with pytest.raises(ValueError):
        Fault(Blame.IDENTITY, until=until)
    with pytest.raises(ValueError):
        Fault(Blame.IDENTITY_BANNED, standing="frozen", until=until)
    with pytest.raises(ValueError):
        Fault(Blame.IDENTITY_BANNED, until=datetime(2026, 1, 2))
