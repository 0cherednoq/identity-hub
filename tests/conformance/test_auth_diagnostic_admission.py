"""AR2.2: диагностический допуск обходит eligibility нагрузки и ничего больше (D-335, L2)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel

from identity_hub import (
    AuthState,
    Availability,
    DiagnosticPolicy,
    IdentityCreate,
    IdentityHub,
    LeaseUnavailable,
    OperationNotAllowed,
    OperationRule,
    Override,
    ProbeContext,
    ProbeResult,
    Service,
    SpaceHandle,
    Standing,
)
from identity_hub.errors import ValidationFailed
from identity_hub.read.events import EventFilters

pytestmark = [pytest.mark.asyncio, pytest.mark.conformance]


class Session(BaseModel):
    token: str


class AuthProbe:
    async def auth(self, ctx: ProbeContext) -> ProbeResult | None:
        return ProbeResult(AuthState.AUTHENTICATED)


async def _identity(
    hub: IdentityHub,
    space: SpaceHandle,
    name: str,
    *,
    lifecycle: str = "ready",
    availability: str = "available",
    standing: str = "good",
    policy: DiagnosticPolicy | None = None,
    operations: dict[str, OperationRule] | None = None,
) -> UUID:
    hub.register_service(
        Service(
            name, name, session=Session, probes=AuthProbe(), diagnostics=policy, operations=operations or {}
        )
    )
    await hub.start()
    created = await space.identities.create(
        IdentityCreate(
            service=name, external_ref=f"{name}-1", lifecycle="provisioning", session={"token": "t"}
        ),
        actor="test",
    )
    identity_id = created.id
    if lifecycle in {"ready", "retired"}:
        await space.identities.activate(identity_id, actor="test")
    if lifecycle == "retired":
        await space.identities.retire(identity_id, actor="test", reason="x")
    if availability != "available":
        await space.identities.override(
            identity_id,
            Override("availability", "", Availability(availability), reason="setup", actor="test"),
        )
    if standing != "good":
        await space.identities.override(
            identity_id, Override("standing", "", Standing(standing), reason="setup", actor="test")
        )
    return identity_id


async def _diagnose(space: SpaceHandle, identity_id: UUID, kind: str = "probe") -> None:
    lease = await space._diagnostic_lease(identity_id, actor="checker", reason="operator recheck")
    try:
        async with lease.operation(kind):
            pass
    finally:
        await lease.release()


@pytest.mark.parametrize(
    ("lifecycle", "availability", "standing"),
    [
        ("ready", "available", "good"),
        ("provisioning", "available", "good"),
        ("retired", "available", "good"),
        ("ready", "paused", "good"),
        ("ready", "cooling", "good"),
        ("ready", "available", "banned"),
        ("ready", "available", "frozen"),
        ("retired", "cooling", "banned"),
    ],
)
async def test_default_policy_admits_diagnostics(
    hub_under_test: IdentityHub, space: SpaceHandle, lifecycle: str, availability: str, standing: str
) -> None:
    identity_id = await _identity(
        hub_under_test,
        space,
        f"da_ok_{lifecycle}_{availability}_{standing}",
        lifecycle=lifecycle,
        availability=availability,
        standing=standing,
    )

    await _diagnose(space, identity_id)

    identity = await space.identities.get(identity_id)
    assert (identity.lifecycle, identity.availability, identity.standing) == (
        lifecycle,
        availability,
        standing,
    )  # диагностика ничего не меняет


@pytest.mark.parametrize(
    ("availability", "standing", "code"),
    [("quarantined", "good", "availability"), ("available", "deleted", "standing")],
)
async def test_quarantined_and_deleted_are_blocked_by_default_with_typed_blocker(
    hub_under_test: IdentityHub, space: SpaceHandle, availability: str, standing: str, code: str
) -> None:
    identity_id = await _identity(
        hub_under_test,
        space,
        f"da_block_{availability}_{standing}",
        availability=availability,
        standing=standing,
    )

    with pytest.raises(OperationNotAllowed) as caught:
        await space._diagnostic_lease(identity_id, actor="checker", reason="recheck")

    assert [blocker.code for blocker in caught.value.blockers] == [code]


async def test_service_can_admit_quarantined_and_deleted_explicitly(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    policy = DiagnosticPolicy(availability=frozenset(Availability), standing=frozenset(Standing))
    identity_id = await _identity(
        hub_under_test, space, "da_explicit", availability="quarantined", standing="deleted", policy=policy
    )

    await _diagnose(space, identity_id)

    assert (await space.identities.get(identity_id)).availability == Availability.QUARANTINED


async def test_ordinary_sync_paths_are_unchanged(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    retired = await _identity(hub_under_test, space, "da_ordinary", lifecycle="retired")
    with pytest.raises(LeaseUnavailable):
        await space.sync.refresh(retired)
    report = await space.sync.refresh_many(service="da_ordinary", not_synced_for=timedelta(0))
    assert retired not in report.probed


async def test_diagnostic_bypass_is_limited_to_probe_operations(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    identity_id = await _identity(
        hub_under_test,
        space,
        "da_probe_only",
        lifecycle="retired",
        operations={"send": OperationRule(needs_auth=False)},
    )

    with pytest.raises(OperationNotAllowed):
        await _diagnose(space, identity_id, kind="send")


async def test_second_diagnostic_on_same_identity_is_busy(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    identity_id = await _identity(hub_under_test, space, "da_busy", lifecycle="retired")
    first = await space._diagnostic_lease(identity_id, actor="checker", reason="recheck")
    try:
        with pytest.raises(LeaseUnavailable):
            await space._diagnostic_lease(identity_id, actor="checker", reason="recheck")
    finally:
        await first.release()


@pytest.mark.parametrize(("actor", "reason"), [("", "why"), ("checker", ""), ("checker", "   ")])
async def test_actor_and_reason_are_required(
    hub_under_test: IdentityHub, space: SpaceHandle, actor: str, reason: str
) -> None:
    identity_id = await _identity(hub_under_test, space, f"da_req_{len(actor)}_{len(reason)}")
    with pytest.raises(ValidationFailed):
        await space._diagnostic_lease(identity_id, actor=actor, reason=reason)


async def test_unknown_identity_is_a_typed_blocker(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    await _identity(hub_under_test, space, "da_unknown")
    with pytest.raises(OperationNotAllowed) as caught:
        await space._diagnostic_lease(UUID(int=1), actor="checker", reason="recheck")
    assert [blocker.code for blocker in caught.value.blockers] == ["not_found"]


async def test_diagnostic_start_is_audited_without_secrets(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    identity_id = await _identity(hub_under_test, space, "da_audit", lifecycle="retired")
    await _diagnose(space, identity_id)

    events = await space.admin.event_page(
        filters=EventFilters(identity_id=identity_id, include_journal=True), limit=100
    )

    started: list[Any] = [item for item in events.items if item.type == "identity.diagnostic.started"]
    assert len(started) == 1
    assert started[0].actor == "checker"
    assert started[0].data == {"reason": "operator recheck"}
