"""AR2.1: DiagnosticPolicy и типизированные Blocker (D-335, L2)."""

from __future__ import annotations

import pytest

from identity_hub import (
    Availability,
    Blocker,
    BlockerKind,
    DiagnosticPolicy,
    Lifecycle,
    Service,
    Standing,
)
from identity_hub.errors import ValidationFailed

pytestmark = pytest.mark.unit


def test_default_policy_allows_retired_paused_cooling_but_not_quarantined_or_deleted() -> None:
    policy = DiagnosticPolicy()
    assert policy.lifecycles == {Lifecycle.PROVISIONING, Lifecycle.READY, Lifecycle.RETIRED}
    assert policy.availability == {Availability.AVAILABLE, Availability.PAUSED, Availability.COOLING}
    assert Availability.QUARANTINED not in policy.availability
    assert Standing.DELETED not in policy.standing
    assert {
        Standing.GOOD,
        Standing.LIMITED,
        Standing.FROZEN,
        Standing.APPEALING,
        Standing.BANNED,
    } <= policy.standing


def test_quarantined_and_deleted_only_when_service_lists_them() -> None:
    policy = DiagnosticPolicy(availability=frozenset(Availability), standing=frozenset(Standing))
    assert Availability.QUARANTINED in policy.availability
    assert Standing.DELETED in policy.standing


def test_values_are_normalized_from_strings() -> None:
    policy = DiagnosticPolicy(lifecycles=frozenset({"ready"}), availability=frozenset({"available"}))
    assert policy.lifecycles == frozenset({Lifecycle.READY})
    assert all(isinstance(item, Lifecycle) for item in policy.lifecycles)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"lifecycles": frozenset({"gone"})},
        {"availability": frozenset({"gone"})},
        {"standing": frozenset({"gone"})},
        {"lifecycles": frozenset()},
        {"availability": frozenset()},
        {"standing": frozenset()},
    ],
)
def test_invalid_policy_is_rejected(kwargs: dict[str, frozenset[str]]) -> None:
    with pytest.raises(ValidationFailed):
        DiagnosticPolicy(**kwargs)  # type: ignore[arg-type]


def test_blockers_name_every_failing_axis() -> None:
    policy = DiagnosticPolicy()
    assert policy.blockers(Lifecycle.RETIRED, Availability.COOLING, Standing.BANNED) == ()
    found = policy.blockers(Lifecycle.READY, Availability.QUARANTINED, Standing.DELETED)
    assert {blocker.code for blocker in found} == {BlockerKind.AVAILABILITY, BlockerKind.STANDING}
    assert {(b.code, b.detail) for b in found} == {("availability", "quarantined"), ("standing", "deleted")}
    narrow = DiagnosticPolicy(lifecycles=frozenset({Lifecycle.READY}))
    assert narrow.blockers(Lifecycle.RETIRED, Availability.AVAILABLE, Standing.GOOD) == (
        Blocker("lifecycle", "retired"),
    )


def test_blocker_kinds_cover_plan() -> None:
    assert {kind.value for kind in BlockerKind} == {
        "lifecycle",
        "availability",
        "standing",
        "credential_missing",
        "credential_expired",
        "credential_disabled",
        "credential_revoked",
        "proxy",
        "busy",
        "not_found",
    }


def test_service_uses_default_policy_unless_declared() -> None:
    assert Service("diag_default", "Diag").diagnostic_policy() == DiagnosticPolicy()
    explicit = DiagnosticPolicy(availability=frozenset(Availability))
    assert Service("diag_explicit", "Diag", diagnostics=explicit).diagnostic_policy() is explicit


def test_service_rejects_wrong_diagnostics_type() -> None:
    with pytest.raises(ValidationFailed):
        Service("diag_bad", "Diag", diagnostics="all")  # type: ignore[arg-type]
