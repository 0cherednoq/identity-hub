"""SF2.1: публичная адресная проверка session slot."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from identity_hub import (
    AuthState,
    IdentityCreate,
    IdentityHub,
    Lease,
    ProbeContext,
    ProbeResult,
    Service,
    SessionAdoptionOutcome,
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


async def test_adopt_named_session_sets_canonical_auth_and_activates(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    probe, created = await _setup(
        hub_under_test,
        space,
        "sf_adopt_valid",
        ProbeResult(AuthState.AUTHENTICATED),
    )

    result = await space.auth.adopt_session(
        created.id,
        "cookie-1",
        actor="checker",
        activate=True,
    )

    assert result.outcome is SessionAdoptionOutcome.ADOPTED
    assert result.auth == AuthState.AUTHENTICATED and result.lifecycle == "ready"
    assert probe.calls == [("cookie-1", Session(token="cookie-token"))]
    identity = await space.identities.get(created.id)
    assert identity.auth == AuthState.AUTHENTICATED and identity.lifecycle == "ready"
    async with space._tx(own=True) as tx:
        source = (await tx.session_history(created.id, "cookie-1"))[-1]
        default = await tx.session_history(created.id, "default")
    assert source.last_verified_at is not None
    assert len(default) == 1 and default[0].status == "active"
    assert default[0].last_verified_at is not None


async def test_adopt_invalid_session_does_not_create_default_or_flip_auth(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    probe, created = await _setup(
        hub_under_test,
        space,
        "sf_adopt_invalid",
        ProbeResult(AuthState.SESSION_INVALID),
    )

    result = await space.auth.adopt_session(
        created.id,
        "cookie-1",
        actor="checker",
        activate=True,
    )

    assert result.outcome is SessionAdoptionOutcome.INVALID
    identity = await space.identities.get(created.id)
    assert identity.auth == AuthState.NO_SESSION and identity.lifecycle == "provisioning"
    async with space._tx(own=True) as tx:
        assert await tx.session_history(created.id, "default") == []
    assert len(probe.calls) == 1


async def test_adopt_technical_failure_has_no_partial_state(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    probe, created = await _setup(
        hub_under_test,
        space,
        "sf_adopt_failure",
        ConnectionError("provider unavailable"),
    )

    result = await space.auth.adopt_session(
        created.id,
        "cookie-1",
        actor="checker",
        activate=True,
    )

    assert result.outcome is SessionAdoptionOutcome.FAILED and result.retryable
    row = await _current_session(space, created.id)
    assert row.last_verified_at is None and row.last_rejected_at is None
    assert (await space.identities.get(created.id)).auth == AuthState.NO_SESSION
    async with space._tx(own=True) as tx:
        assert await tx.session_history(created.id, "default") == []
    assert len(probe.calls) == 1


async def test_adopt_is_idempotent_for_default_history(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    probe, created = await _setup(
        hub_under_test,
        space,
        "sf_adopt_repeat",
        ProbeResult(AuthState.AUTHENTICATED),
    )

    first = await space.auth.adopt_session(created.id, "cookie-1", actor="checker")
    second = await space.auth.adopt_session(created.id, "cookie-1", actor="checker")

    assert first.outcome is second.outcome is SessionAdoptionOutcome.ADOPTED
    async with space._tx(own=True) as tx:
        default = await tx.session_history(created.id, "default")
    assert len(default) == 1
    assert len(probe.calls) == 2


async def test_adopt_default_does_not_replace_its_own_history(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    probe, created = await _setup(
        hub_under_test,
        space,
        "sf_adopt_default",
        ProbeResult(AuthState.AUTHENTICATED),
    )
    await space.secrets.replace_session(
        created.id,
        {"token": "default-token"},
        actor="test",
        slot="default",
    )

    result = await space.auth.adopt_session(created.id, actor="checker")

    assert result.outcome is SessionAdoptionOutcome.ADOPTED
    async with space._tx(own=True) as tx:
        default = await tx.session_history(created.id, "default")
    assert len(default) == 1 and default[0].last_verified_at is not None
    assert probe.calls == [("default", Session(token="default-token"))]


async def test_adopt_final_transaction_rolls_back_as_a_unit(
    hub_under_test: IdentityHub,
    space: SpaceHandle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe, created = await _setup(
        hub_under_test,
        space,
        "sf_adopt_rollback",
        ProbeResult(AuthState.AUTHENTICATED),
    )
    original = Lease._write_session

    async def fail_after_default_write(self: Lease[Any, Any], *args: Any, **kwargs: Any) -> None:
        await original(self, *args, **kwargs)
        raise RuntimeError("injected final transaction failure")

    monkeypatch.setattr(Lease, "_write_session", fail_after_default_write)

    with pytest.raises(RuntimeError, match="injected final transaction failure"):
        await space.auth.adopt_session(
            created.id,
            "cookie-1",
            actor="checker",
            activate=True,
        )

    source = await _current_session(space, created.id)
    assert source.last_verified_at is None and source.last_rejected_at is None
    async with space._tx(own=True) as tx:
        assert await tx.session_history(created.id, "default") == []
    identity = await space.identities.get(created.id)
    assert identity.auth == AuthState.NO_SESSION and identity.lifecycle == "provisioning"
    assert len(probe.calls) == 1
