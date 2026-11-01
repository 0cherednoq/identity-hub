"""AR3.1: `auth.revalidate()` — шаг сессии, допуск и структурный результат (D-335, L3)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel

from identity_hub import (
    AuthRevalidationResult,
    AuthState,
    Availability,
    IdentityCreate,
    IdentityHub,
    Override,
    ProbeContext,
    ProbeResult,
    RevalidationStatus,
    Service,
    SessionVerdict,
    SpaceHandle,
    Standing,
)
from identity_hub.errors import ValidationFailed
from identity_hub.read.events import EventFilters

pytestmark = [pytest.mark.asyncio, pytest.mark.conformance]

SECRET = "revalidate-secret-token"


class Session(BaseModel):
    token: str


class Probe:
    def __init__(self, auth: ProbeResult | BaseException) -> None:
        self.auth_result = auth
        self.auth_calls = 0

    async def auth(self, ctx: ProbeContext) -> ProbeResult | None:
        self.auth_calls += 1
        if isinstance(self.auth_result, BaseException):
            raise self.auth_result
        return self.auth_result


class StandingProbe(Probe):
    def __init__(self, auth: ProbeResult, standing: ProbeResult) -> None:
        super().__init__(auth)
        self.standing_result = standing
        self.standing_calls = 0

    async def standing(self, ctx: ProbeContext) -> ProbeResult | None:
        self.standing_calls += 1
        return self.standing_result


async def _setup(
    hub: IdentityHub,
    space: SpaceHandle,
    name: str,
    probe: Probe,
    *,
    with_session: bool = True,
    lifecycle: str = "ready",
    availability: str = "available",
    standing: str = "good",
    auth: AuthState | None = None,
) -> UUID:
    hub.register_service(Service(name, name, session=Session, probes=probe))
    await hub.start()
    spec = IdentityCreate(
        service=name,
        external_ref=f"{name}-1",
        lifecycle="provisioning",
        session={"token": SECRET} if with_session else None,
    )
    identity_id = (await space.identities.create(spec, actor="test")).id
    if lifecycle in {"ready", "retired"}:
        await space.identities.activate(identity_id, actor="test")
    if lifecycle == "retired":
        await space.identities.retire(identity_id, actor="test", reason="x")
    if availability != "available":
        await space.identities.override(
            identity_id, Override("availability", "", Availability(availability), reason="s", actor="test")
        )
    if standing != "good":
        await space.identities.override(
            identity_id, Override("standing", "", Standing(standing), reason="s", actor="test")
        )
    if auth is not None and (await space.identities.get(identity_id)).auth != auth:
        await space.identities.override(identity_id, Override("auth", "", auth, reason="s", actor="test"))
    return identity_id


async def _revalidate(
    space: SpaceHandle, identity_id: UUID, request_id: str = "req-1"
) -> AuthRevalidationResult:
    return await space.auth.revalidate(
        identity_id, actor="checker", reason="operator recheck", request_id=request_id
    )


async def _operations(space: SpaceHandle, identity_id: UUID) -> list[Any]:
    async with space._tx(own=True) as tx:
        return await tx.list_provider_operations(identity_id=identity_id, states=None, limit=20)


async def test_valid_session_authenticates_and_marks_metadata(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    probe = Probe(ProbeResult(AuthState.AUTHENTICATED))
    identity_id = await _setup(hub_under_test, space, "rv_valid", probe, auth=AuthState.SESSION_INVALID)

    result = await _revalidate(space, identity_id)

    assert result.status is RevalidationStatus.COMPLETED
    assert result.session_verdict is SessionVerdict.VALID
    assert result.applied == {"auth": "applied"}
    assert result.blockers == () and result.retryable is False
    assert (await space.identities.get(identity_id)).auth == AuthState.AUTHENTICATED
    (operation,) = await _operations(space, identity_id)
    assert (operation.kind, operation.state) == ("probe:auth", "succeeded")
    assert result.operation_ids == (operation.id,)
    assert probe.auth_calls == 1


async def test_invalid_session_marks_session_invalid(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    probe = Probe(ProbeResult(AuthState.SESSION_INVALID))
    identity_id = await _setup(hub_under_test, space, "rv_invalid", probe)

    result = await _revalidate(space, identity_id)

    assert result.status is RevalidationStatus.COMPLETED
    assert result.session_verdict is SessionVerdict.INVALID
    assert result.applied == {"auth": "applied"}
    assert (await space.identities.get(identity_id)).auth == AuthState.SESSION_INVALID
    async with space._tx(own=True) as tx:
        (default,) = await tx.session_history(identity_id, "default")
    assert default.last_rejected_at is not None


async def test_technical_failure_keeps_last_reliable_state(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    probe = Probe(ConnectionError("provider down"))
    identity_id = await _setup(
        hub_under_test, space, "rv_failure", probe, standing="banned", auth=AuthState.SESSION_INVALID
    )

    result = await _revalidate(space, identity_id)

    assert result.status is RevalidationStatus.FAILED
    assert result.session_verdict is SessionVerdict.FAILED
    assert result.retryable is True and result.applied == {}
    identity = await space.identities.get(identity_id)
    assert (identity.auth, identity.standing) == (AuthState.SESSION_INVALID, Standing.BANNED)
    (operation,) = await _operations(space, identity_id)
    assert operation.state == "failed"


@pytest.mark.parametrize(
    ("lifecycle", "availability", "standing"),
    [
        ("ready", "available", "banned"),
        ("retired", "available", "good"),
        ("ready", "paused", "good"),
        ("ready", "cooling", "good"),
        ("retired", "cooling", "banned"),
    ],
)
async def test_blocked_states_are_diagnosed_without_changing_them(
    hub_under_test: IdentityHub, space: SpaceHandle, lifecycle: str, availability: str, standing: str
) -> None:
    probe = Probe(ProbeResult(AuthState.AUTHENTICATED))
    identity_id = await _setup(
        hub_under_test,
        space,
        f"rv_state_{lifecycle}_{availability}_{standing}",
        probe,
        lifecycle=lifecycle,
        availability=availability,
        standing=standing,
    )

    result = await _revalidate(space, identity_id)

    assert result.status is RevalidationStatus.COMPLETED
    assert result.session_verdict is SessionVerdict.VALID
    identity = await space.identities.get(identity_id)
    assert (identity.lifecycle, identity.availability, identity.standing) == (
        lifecycle,
        availability,
        standing,
    )
    assert probe.auth_calls == 1


@pytest.mark.parametrize(
    ("availability", "standing", "code"),
    [("quarantined", "good", "availability"), ("available", "deleted", "standing")],
)
async def test_quarantined_and_deleted_are_blocked_without_provider_call(
    hub_under_test: IdentityHub, space: SpaceHandle, availability: str, standing: str, code: str
) -> None:
    probe = Probe(ProbeResult(AuthState.AUTHENTICATED))
    identity_id = await _setup(
        hub_under_test,
        space,
        f"rv_block_{availability}_{standing}",
        probe,
        availability=availability,
        standing=standing,
    )

    result = await _revalidate(space, identity_id)

    assert result.status is RevalidationStatus.BLOCKED
    assert [blocker.code for blocker in result.blockers] == [code]
    assert result.session_verdict is SessionVerdict.NOT_CHECKED
    assert probe.auth_calls == 0
    assert await _operations(space, identity_id) == []


async def test_unknown_identity_is_blocked_not_found(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    await _setup(hub_under_test, space, "rv_unknown", Probe(ProbeResult(AuthState.AUTHENTICATED)))

    result = await _revalidate(space, UUID(int=7))

    assert result.status is RevalidationStatus.BLOCKED
    assert [blocker.code for blocker in result.blockers] == ["not_found"]


async def test_missing_default_session_is_reported_without_provider_call(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    probe = Probe(ProbeResult(AuthState.AUTHENTICATED))
    identity_id = await _setup(hub_under_test, space, "rv_missing", probe, with_session=False)

    result = await _revalidate(space, identity_id)

    assert result.status is RevalidationStatus.COMPLETED
    assert result.session_verdict is SessionVerdict.MISSING
    assert probe.auth_calls == 0 and result.applied == {} and result.operation_ids == ()


@pytest.mark.parametrize("verdict", [AuthState.AUTHENTICATED, AuthState.SESSION_INVALID])
async def test_active_challenge_is_never_overwritten_or_closed(
    hub_under_test: IdentityHub, space: SpaceHandle, verdict: AuthState
) -> None:
    probe = Probe(ProbeResult(verdict))
    identity_id = await _setup(
        hub_under_test, space, f"rv_challenge_{verdict.value}", probe, auth=AuthState.CHALLENGE
    )

    result = await _revalidate(space, identity_id)

    assert result.applied == {"auth": "sticky"}
    assert (await space.identities.get(identity_id)).auth == AuthState.CHALLENGE


async def test_standing_probe_is_reported_and_banned_stays_banned(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    probe = StandingProbe(ProbeResult(AuthState.AUTHENTICATED), ProbeResult(Standing.GOOD))
    identity_id = await _setup(hub_under_test, space, "rv_standing", probe, standing="banned")

    result = await _revalidate(space, identity_id)

    assert result.applied == {"auth": "unchanged", "standing": "sticky"}
    assert (await space.identities.get(identity_id)).standing == Standing.BANNED
    assert probe.standing_calls == 1


async def test_standing_probe_can_discover_a_ban(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    probe = StandingProbe(ProbeResult(AuthState.AUTHENTICATED), ProbeResult(Standing.BANNED))
    identity_id = await _setup(hub_under_test, space, "rv_discover", probe)

    result = await _revalidate(space, identity_id)

    assert result.applied["standing"] == "applied"
    assert (await space.identities.get(identity_id)).standing == Standing.BANNED


async def test_busy_identity_reports_in_progress_without_provider_call(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    probe = Probe(ProbeResult(AuthState.AUTHENTICATED))
    identity_id = await _setup(hub_under_test, space, "rv_busy", probe)
    held = await space._diagnostic_lease(identity_id, actor="other", reason="holding")
    try:
        result = await _revalidate(space, identity_id)
    finally:
        await held.release()

    assert result.status is RevalidationStatus.IN_PROGRESS
    assert [blocker.code for blocker in result.blockers] == ["busy"]
    assert probe.auth_calls == 0


async def test_result_and_events_contain_no_secrets(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    probe = Probe(ProbeResult(AuthState.SESSION_INVALID))
    identity_id = await _setup(hub_under_test, space, "rv_secret", probe)

    result = await _revalidate(space, identity_id)
    events = await space.admin.event_page(
        filters=EventFilters(identity_id=identity_id, include_journal=True), limit=100
    )

    assert SECRET not in repr(result) and SECRET not in repr(events.items)
    assert any(item.type == "identity.diagnostic.started" for item in events.items)


@pytest.mark.parametrize("request_id", ["", "   ", "x" * 129])
async def test_request_id_is_required(
    hub_under_test: IdentityHub, space: SpaceHandle, request_id: str
) -> None:
    identity_id = await _setup(
        hub_under_test, space, f"rv_req_{len(request_id)}", Probe(ProbeResult(AuthState.AUTHENTICATED))
    )
    with pytest.raises(ValidationFailed):
        await _revalidate(space, identity_id, request_id)


async def test_service_without_auth_probe_is_rejected(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    hub_under_test.register_service(Service("rv_noprobe", "rv_noprobe", session=Session))
    await hub_under_test.start()
    created = await space.identities.create(
        IdentityCreate(service="rv_noprobe", external_ref="x", lifecycle="provisioning"), actor="test"
    )
    with pytest.raises(ValidationFailed):
        await _revalidate(space, created.id)
