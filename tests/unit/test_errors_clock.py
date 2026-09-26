from __future__ import annotations

import pickle
from datetime import timedelta
from uuid import uuid4

import pytest

from identity_hub.clock import FakeClock, SystemClock
from identity_hub.errors import (
    AlreadyExists,
    Blocker,
    IdentityHubError,
    LeaseLost,
    OperationNotAllowed,
    StaleFencing,
    TransitionNotAllowed,
)

pytestmark = pytest.mark.unit


def test_errors_are_picklable_and_keep_fields() -> None:
    for exc in (
        LeaseLost(uuid4(), "heartbeat"),
        StaleFencing(uuid4(), 3, 4),
        AlreadyExists("identity", "tg:+1", uuid4()),
        OperationNotAllowed("send", [Blocker("auth", "no_session")]),
        TransitionNotAllowed("standing", "banned", "good", "нужен override"),
    ):
        restored = pickle.loads(pickle.dumps(exc))
        assert type(restored) is type(exc)
        assert str(restored) == str(exc)
        assert restored.__dict__ == exc.__dict__
        assert isinstance(restored, IdentityHubError)


def test_operation_not_allowed_lists_blockers() -> None:
    exc = OperationNotAllowed("send", [Blocker("auth", "no_session"), Blocker("quota", "send/3600")])
    assert "auth: no_session" in str(exc)
    assert "quota: send/3600" in str(exc)
    assert len(exc.blockers) == 2


def test_fake_clock_advances_both_clocks() -> None:
    clock = FakeClock()
    start, mono = clock.now(), clock.monotonic()
    clock.advance(timedelta(seconds=30))
    clock.advance(1.5)
    assert clock.now() - start == timedelta(seconds=31.5)
    assert clock.monotonic() - mono == 31.5


def test_system_clock_is_aware() -> None:
    assert SystemClock().now().tzinfo is not None
