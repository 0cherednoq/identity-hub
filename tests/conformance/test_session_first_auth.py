"""SF2.1: публичная адресная проверка session slot."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from identity_hub import (
    AuthState,
    IdentityCreate,
    IdentityHub,
    ProbeContext,
    ProbeResult,
    Service,
    SessionVerificationOutcome,
    SpaceHandle,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.conformance]


class Session(BaseModel):
    token: str


class AuthProbe:
    def __init__(self, result: ProbeResult | BaseException) -> None:
        self.result = result
        self.calls: list[tuple[str, Session | None]] = []

    async def auth(self, ctx: ProbeContext) -> ProbeResult | None:
        value = await ctx.session()
        self.calls.append((ctx.session_slot, value if isinstance(value, Session) else None))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


async def _setup(
    hub: IdentityHub,
    space: SpaceHandle,
    name: str,
    result: ProbeResult | BaseException,
) -> tuple[AuthProbe, Any]:
    probe = AuthProbe(result)
    service = Service(name, name, session=Session, probes=probe)
    hub.register_service(service)
    await hub.start()
    created = await space.identities.create(
        IdentityCreate(
            service=name,
            external_ref=f"{name}-1",
            lifecycle="provisioning",
            sessions={"cookie-1": {"token": "cookie-token"}},
        ),
        actor="test",
    )
    return probe, created


async def _current_session(space: SpaceHandle, identity_id: Any) -> Any:
    async with space._tx(own=True) as tx:
        return (await tx.session_history(identity_id, "cookie-1"))[-1]


async def test_verify_named_session_marks_metadata_only(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    probe, created = await _setup(
        hub_under_test,
        space,
        "sf_verify_valid",
        ProbeResult(AuthState.AUTHENTICATED),
    )

    result = await space.auth.verify_session(created.id, "cookie-1", actor="checker")

    assert result.outcome is SessionVerificationOutcome.VALID
    assert probe.calls == [("cookie-1", Session(token="cookie-token"))]
    assert (await _current_session(space, created.id)).last_verified_at is not None
    identity = await space.identities.get(created.id)
    assert identity.auth == AuthState.NO_SESSION
    assert identity.lifecycle == "provisioning"


async def test_verify_invalid_session_rejects_only_source_metadata(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    probe, created = await _setup(
        hub_under_test,
        space,
        "sf_verify_invalid",
        ProbeResult(AuthState.SESSION_INVALID, code="expired"),
    )

    result = await space.auth.verify_session(created.id, "cookie-1", actor="checker")

    assert result.outcome is SessionVerificationOutcome.INVALID
    row = await _current_session(space, created.id)
    assert row.last_rejected_at is not None and row.reject_code == "session_invalid"
    assert (await space.identities.get(created.id)).auth == AuthState.NO_SESSION
    assert len(probe.calls) == 1


async def test_verify_technical_failure_is_retryable_without_metadata_change(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    probe, created = await _setup(
        hub_under_test,
        space,
        "sf_verify_failure",
        ConnectionError("provider unavailable"),
    )

    result = await space.auth.verify_session(created.id, "cookie-1", actor="checker")

    assert result.outcome is SessionVerificationOutcome.FAILED and result.retryable
    row = await _current_session(space, created.id)
    assert row.last_verified_at is None and row.last_rejected_at is None
    assert len(probe.calls) == 1


async def test_verify_missing_slot_does_not_call_provider(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    probe, created = await _setup(
        hub_under_test,
        space,
        "sf_verify_missing",
        ProbeResult(AuthState.AUTHENTICATED),
    )

    result = await space.auth.verify_session(created.id, "missing", actor="checker")

    assert result.outcome is SessionVerificationOutcome.INVALID
    assert probe.calls == []
