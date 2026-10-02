"""P1.3: верификации (ТЗ §3.5, FR-10)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from identity_hub.domain.regions import Observation, Outcome
from identity_hub.domain.verification import (
    VERIFICATION,
    VerificationFlow,
    VerificationMode,
    VerificationSpec,
    VerificationTier,
    achieved_tiers,
    effective_factors,
    required_factors,
    verified_factors_after,
)
from identity_hub.enums import ChangeSource
from identity_hub.enums import VerificationState as V
from identity_hub.errors import (
    AttemptsExhausted,
    TransitionNotAllowed,
    ValidationFailed,
    VerificationRequirementsNotMet,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 1, 1, tzinfo=UTC)

KYC = VerificationSpec(
    name="kyc_id",
    requires=frozenset({"email"}),
    mode=VerificationMode.MANUAL,
    valid_for=timedelta(days=365),
    max_attempts=2,
    reject_codes=frozenset({"blurry", "expired_document"}),
)
EMAIL = VerificationSpec(name="email", mode=VerificationMode.AUTO)


def t(n: float) -> datetime:
    return T0 + timedelta(seconds=n)


def test_manual_full_cycle() -> None:
    flow = VerificationFlow(KYC)
    s = flow.initial(T0)
    s = flow.start(s, {"email"}, t(1), "w").status
    assert s.state == V.PENDING
    assert s.started_at == t(1)
    submitted = flow.submit(s, t(2), "w")
    assert submitted.status.state == V.UNDER_REVIEW
    assert submitted.status.attempts == 1
    approved = flow.approve(submitted.status, t(3), "platform")
    assert approved.status.state == V.VERIFIED
    assert approved.status.expires_at == t(3) + timedelta(days=365)
    assert approved.region is not None
    (event,) = approved.region.events
    assert event.type == "identity.verification.changed"
    assert event.data["key"] == "kyc_id"
    assert event.data["to"] == V.VERIFIED


def test_auto_mode_submit_verifies() -> None:
    flow = VerificationFlow(EMAIL)
    s = flow.start(flow.initial(T0), set(), t(1), "w").status
    decision = flow.submit(s, t(2), "w")
    assert decision.status.state == V.VERIFIED
    assert decision.status.verified_at == t(2)
    assert decision.status.expires_at is None


def test_start_requires_dependencies() -> None:
    flow = VerificationFlow(KYC)
    with pytest.raises(VerificationRequirementsNotMet) as exc:
        flow.start(flow.initial(T0), set(), t(1), "w")
    assert exc.value.missing == ["email"]


def test_start_from_pending_resumes_without_attempt() -> None:
    flow = VerificationFlow(KYC)
    s = flow.start(flow.initial(T0), {"email"}, t(1), "w").status
    again = flow.start(s, {"email"}, t(2), "w")
    assert not again.changed
    assert again.region is None
    assert again.status.attempts == 0


def test_attempt_limit() -> None:
    flow = VerificationFlow(KYC)
    s = flow.initial(T0)
    for n in range(2):
        s = flow.start(s, {"email"}, t(10 * n + 1), "w").status
        s = flow.submit(s, t(10 * n + 2), "w").status
        s = flow.reject(s, "blurry", t(10 * n + 3), "platform").status
    assert s.attempts == 2
    assert s.state == V.REJECTED
    assert s.region.code == "blurry"
    with pytest.raises(AttemptsExhausted):
        flow.start(s, {"email"}, t(100), "w")


def test_reject_code_must_be_declared() -> None:
    flow = VerificationFlow(KYC)
    s = flow.start(flow.initial(T0), {"email"}, t(1), "w").status
    s = flow.submit(s, t(2), "w").status
    with pytest.raises(ValidationFailed):
        flow.reject(s, "unknown", t(3), "platform")


def test_submit_only_from_pending() -> None:
    flow = VerificationFlow(KYC)
    with pytest.raises(TransitionNotAllowed):
        flow.submit(flow.initial(T0), t(1), "w")


def test_abandon_and_restart() -> None:
    flow = VerificationFlow(KYC)
    s = flow.start(flow.initial(T0), {"email"}, t(1), "w").status
    s = flow.abandon(s, t(2), "w").status
    assert s.state == V.MISSING
    assert flow.start(s, {"email"}, t(3), "w").status.state == V.PENDING


def test_expiry_and_renewal() -> None:
    flow = VerificationFlow(KYC)
    s = flow.start(flow.initial(T0), {"email"}, t(1), "w").status
    s = flow.approve(flow.submit(s, t(2), "w").status, t(3), "p").status
    expires = s.expires_at
    assert expires is not None
    assert flow.expire(s, expires - timedelta(seconds=1)).region.outcome is Outcome.NOT_DUE  # type: ignore[union-attr]
    s = flow.expire(s, expires).status
    assert s.state == V.EXPIRED
    assert flow.start(s, {"email"}, expires, "w").status.state == V.PENDING


def test_pending_timeout() -> None:
    spec = VerificationSpec(name="phone", pending_timeout=timedelta(minutes=10))
    flow = VerificationFlow(spec)
    s = flow.start(flow.initial(T0), set(), t(0), "w").status
    assert flow.expire(s, t(600)).status.state == V.MISSING


def test_reconcile_normalizes_missing_to_revoked() -> None:
    flow = VerificationFlow(KYC)
    s = flow.start(flow.initial(T0), {"email"}, t(1), "w").status
    s = flow.approve(flow.submit(s, t(2), "w").status, t(3), "p").status
    first = flow.reconcile(s, Observation(V.MISSING, observed_at=t(4), source=ChangeSource.PROBE), t(4))
    assert first.status.state == V.VERIFIED  # R2-F13: одна проба подтверждение не снимает
    decision = flow.reconcile(
        first.status, Observation(V.MISSING, observed_at=t(5), source=ChangeSource.PROBE), t(5)
    )
    assert decision.status.state == V.REVOKED


def test_reconcile_verified_sets_validity() -> None:
    flow = VerificationFlow(KYC)
    decision = flow.reconcile(flow.initial(T0), Observation(V.VERIFIED, observed_at=t(5)), t(6))
    assert decision.status.state == V.VERIFIED
    assert decision.status.expires_at == t(5) + timedelta(days=365)
    assert decision.status.verified_at == t(6)


def test_region_spec_matches_diagram() -> None:
    arrows = {
        (V.MISSING, V.PENDING),
        (V.PENDING, V.MISSING),
        (V.PENDING, V.UNDER_REVIEW),
        (V.PENDING, V.VERIFIED),
        (V.UNDER_REVIEW, V.VERIFIED),
        (V.UNDER_REVIEW, V.REJECTED),
        (V.REJECTED, V.PENDING),
        (V.VERIFIED, V.EXPIRED),
        (V.VERIFIED, V.REVOKED),
        (V.EXPIRED, V.PENDING),
        (V.REVOKED, V.PENDING),
    }
    actual = {(src, dst) for src, dsts in VERIFICATION.transitions.items() for dst in dsts}
    assert actual == arrows


def test_tiers_and_factor_helpers() -> None:
    tiers = {
        "basic": VerificationTier("basic", frozenset({"email"})),
        "full": VerificationTier("full", frozenset({"email", "kyc_id"})),
    }
    assert achieved_tiers({"email"}, tiers.values()) == {"basic"}
    assert achieved_tiers({"email", "kyc_id"}, tiers.values()) == {"basic", "full"}
    assert required_factors({"phone"}, ["full"], tiers) == {"phone", "email", "kyc_id"}
    with pytest.raises(ValidationFailed):
        required_factors(set(), ["gold"], tiers)
    assert effective_factors({"email", "removed"}, {"email", "kyc_id"}) == {"email"}
    assert verified_factors_after({"email"}, "kyc_id", V.VERIFIED) == {"email", "kyc_id"}
    assert verified_factors_after({"email", "kyc_id"}, "kyc_id", V.REVOKED) == {"email"}


def test_spec_validation() -> None:
    with pytest.raises(ValueError):
        VerificationSpec(name="x", requires=frozenset({"x"}))
    with pytest.raises(ValueError):
        VerificationSpec(name="x", max_attempts=0)
