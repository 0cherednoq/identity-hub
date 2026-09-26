"""P1.5: допустимость операции (ТЗ §3.9)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from identity_hub.domain.admissibility import IdentitySnapshot, QuotaUsage, compile_rule, evaluate
from identity_hub.enums import AuthState, Availability, Lifecycle, Standing
from tests.fixtures.services import FAKE_EXCHANGE, FAKE_TG

pytestmark = pytest.mark.unit

NOW = datetime(2026, 3, 5, 12, 30, tzinfo=UTC)
READY = IdentitySnapshot(
    lifecycle=Lifecycle.READY,
    standing=Standing.GOOD,
    auth_state=AuthState.AUTHENTICATED,
    availability=Availability.AVAILABLE,
)


def codes(blockers: list[object]) -> list[str]:
    return [b.code for b in blockers]  # type: ignore[attr-defined]


def test_ready_identity_is_allowed() -> None:
    assert evaluate(FAKE_TG, "read_channel", READY, NOW) == []


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"lifecycle": Lifecycle.PROVISIONING}, ["lifecycle"]),
        ({"standing": Standing.FROZEN, "standing_code": "frozen"}, ["standing"]),
        ({"auth_state": AuthState.SESSION_INVALID}, ["auth"]),
        ({"availability": Availability.QUARANTINED}, ["availability"]),
        (
            {"availability": Availability.COOLING, "availability_until": NOW + timedelta(seconds=1)},
            ["availability"],
        ),
        (
            {"lifecycle": Lifecycle.RETIRED, "auth_state": AuthState.NO_SESSION},
            ["lifecycle", "auth"],
        ),
    ],
)
def test_blockers(change: dict[str, object], expected: list[str]) -> None:
    identity = replace(READY, **change)  # type: ignore[arg-type]
    assert codes(evaluate(FAKE_TG, "read_channel", identity, NOW)) == expected


def test_expired_temporary_availability_is_lazy_available() -> None:
    identity = replace(READY, availability=Availability.PAUSED, availability_until=NOW)
    assert evaluate(FAKE_TG, "read_channel", identity, NOW) == []


def test_rule_standing_set() -> None:
    limited = replace(READY, standing=Standing.LIMITED)
    assert evaluate(FAKE_TG, "read_channel", limited, NOW) == []
    assert codes(evaluate(FAKE_TG, "send_message", limited, NOW)) == ["standing"]


def test_probe_allowed_without_auth() -> None:
    identity = replace(READY, auth_state=AuthState.NO_SESSION, standing=Standing.BANNED)
    assert evaluate(FAKE_TG, "probe", identity, NOW) == []


def test_verification_tiers_and_removed_factors() -> None:
    partial = replace(READY, verified_factors=frozenset({"email", "phone", "removed_factor"}))
    (blocker,) = evaluate(FAKE_EXCHANGE, "withdraw", partial, NOW)
    assert blocker.code == "verification"
    assert blocker.detail == "kyc_id"
    full = replace(partial, verified_factors=frozenset({"email", "phone", "kyc_id"}))
    assert evaluate(FAKE_EXCHANGE, "withdraw", full, NOW) == []


def test_quota_blocks_only_in_current_window() -> None:
    hour_start = datetime(2026, 3, 5, 12, tzinfo=UTC)
    usage = {("send_message", 3600): QuotaUsage(used=20, window_start=hour_start)}
    (blocker,) = evaluate(FAKE_TG, "send_message", READY, NOW, usage)
    assert blocker.code == "quota"
    assert blocker.until == hour_start + timedelta(hours=1)
    old = {("send_message", 3600): QuotaUsage(used=20, window_start=hour_start - timedelta(hours=1))}
    assert evaluate(FAKE_TG, "send_message", READY, NOW, old) == []


def test_compile_rule() -> None:
    flt = compile_rule(FAKE_EXCHANGE, "withdraw")
    assert flt.required_factors == {"email", "phone", "kyc_id"}
    assert flt.needs_auth
    assert flt.standing == {Standing.GOOD, Standing.LIMITED}
