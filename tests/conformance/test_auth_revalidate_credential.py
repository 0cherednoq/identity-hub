"""AR3.2: `auth.revalidate()` — шаг credential без `logging_in` (D-335, L3)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from pydantic import SecretStr
from tests.fixtures.services import FAKE_TG, TgSession

from identity_hub import (
    AuthRevalidationResult,
    AuthState,
    Blame,
    CredentialVerdict,
    Fault,
    IdentityCreate,
    IdentityHub,
    LoginChallenge,
    LoginFailure,
    Override,
    ProbeContext,
    ProbeResult,
    RevalidationStatus,
    SessionVerdict,
    SpaceHandle,
    Standing,
)
from identity_hub.read.events import EventFilters

pytestmark = [pytest.mark.asyncio, pytest.mark.conformance]

SECRET = "credential-step-secret"
KEY = ("default", "default")


class SessionProbe:
    def __init__(self, state: AuthState = AuthState.SESSION_INVALID) -> None:
        self.state = state
        self.calls = 0

    async def auth(self, ctx: ProbeContext) -> ProbeResult | None:
        self.calls += 1
        return ProbeResult(self.state)


class Authenticator:
    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode
        self.calls = 0
        self.requested: list[tuple[str, str] | None] = []

    async def login(self, ctx: Any) -> TgSession:
        self.calls += 1
        self.requested.append(ctx.requested_credential)
        if self.mode == "reject":
            raise LoginFailure("credentials_rejected", retryable=False, credential=ctx.requested_credential)
        if self.mode == "banned":
            raise LoginFailure("identity_banned", fault=Fault(Blame.IDENTITY_BANNED, code="platform_ban"))
        if self.mode == "tech":
            raise ConnectionError(f"provider down {SECRET}")
        if self.mode == "challenge":
            raise LoginChallenge("sms_code", "Код из SMS", {}, timedelta(minutes=5))
        return TgSession(auth_key=SecretStr(SECRET))


async def _setup(
    hub: IdentityHub,
    space: SpaceHandle,
    name: str,
    *,
    mode: str = "ok",
    session: bool = False,
    probe_state: AuthState = AuthState.SESSION_INVALID,
    lifecycle: str = "ready",
    standing: str = "good",
    auth: AuthState | None = None,
) -> tuple[Authenticator, SessionProbe, UUID]:
    authenticator = Authenticator(mode)
    probe = SessionProbe(probe_state)
    hub.register_service(replace(FAKE_TG, name=name, authenticator=authenticator, probes=probe))
    await hub.start()
    spec = IdentityCreate(
        service=name,
        external_ref=f"{name}-1",
        lifecycle="provisioning",
        credentials={"phone": f"+7{name}"},
        session={"auth_key": "old", "dc_id": 2} if session else None,
    )
    identity_id = (await space.identities.create(spec, actor="test")).id
    if lifecycle in {"ready", "retired"}:
        await space.identities.activate(identity_id, actor="test")
    if lifecycle == "retired":
        await space.identities.retire(identity_id, actor="test", reason="x")
    if standing != "good":
        await space.identities.override(
            identity_id, Override("standing", "", Standing(standing), reason="s", actor="test")
        )
    if auth is not None and (await space.identities.get(identity_id)).auth != auth:
        await space.identities.override(identity_id, Override("auth", "", auth, reason="s", actor="test"))
    return authenticator, probe, identity_id


async def _revalidate(
    space: SpaceHandle, identity_id: UUID, credential: tuple[str, str] | None = KEY, request_id: str = "r-1"
) -> AuthRevalidationResult:
    return await space.auth.revalidate(
        identity_id, actor="checker", reason="operator recheck", request_id=request_id, credential=credential
    )


async def _operations(space: SpaceHandle, identity_id: UUID) -> list[Any]:
    async with space._tx(own=True) as tx:
        return await tx.list_provider_operations(identity_id=identity_id, states=None, limit=20)


async def test_missing_session_falls_back_to_credential_and_authenticates(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    authenticator, probe, identity_id = await _setup(hub_under_test, space, "rc_ok")

    result = await _revalidate(space, identity_id)

    assert result.status is RevalidationStatus.COMPLETED
    assert (result.session_verdict, result.credential_verdict) == (
        SessionVerdict.MISSING,
        CredentialVerdict.VALID,
    )
    assert result.applied["auth"] == "applied"
    assert authenticator.calls == 1 and authenticator.requested == [KEY] and probe.calls == 0
    identity = await space.identities.get(identity_id)
    assert identity.auth == AuthState.AUTHENTICATED
    async with space._tx(own=True) as tx:
        history = await tx.session_history(identity_id, "default")
    assert history and history[-1].status == "active"
    (credential,) = await space.secrets.credentials(identity_id)
    assert credential.last_verified_at is not None
    (operation,) = await _operations(space, identity_id)
    assert (operation.kind, operation.state) == (
        "login",
        "succeeded",
    ) and operation.id in result.operation_ids


async def test_invalid_session_then_credential_in_one_call(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    authenticator, probe, identity_id = await _setup(hub_under_test, space, "rc_invalid", session=True)

    result = await _revalidate(space, identity_id)

    assert (result.session_verdict, result.credential_verdict) == (
        SessionVerdict.INVALID,
        CredentialVerdict.VALID,
    )
    assert (probe.calls, authenticator.calls) == (1, 1)
    assert (await space.identities.get(identity_id)).auth == AuthState.AUTHENTICATED
    assert len(result.operation_ids) == 2


async def test_valid_session_skips_credential(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    authenticator, _probe, identity_id = await _setup(
        hub_under_test, space, "rc_valid", session=True, probe_state=AuthState.AUTHENTICATED
    )

    result = await _revalidate(space, identity_id)

    assert result.session_verdict is SessionVerdict.VALID
    assert result.credential_verdict is CredentialVerdict.NOT_CHECKED and authenticator.calls == 0


async def test_without_credential_argument_no_fallback(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    authenticator, _probe, identity_id = await _setup(hub_under_test, space, "rc_none")

    result = await _revalidate(space, identity_id, credential=None)

    assert result.credential_verdict is CredentialVerdict.NOT_CHECKED and authenticator.calls == 0


@pytest.mark.parametrize(
    ("lifecycle", "standing"), [("retired", "good"), ("ready", "banned"), ("retired", "banned")]
)
async def test_success_keeps_lifecycle_and_standing(
    hub_under_test: IdentityHub, space: SpaceHandle, lifecycle: str, standing: str
) -> None:
    _auth, _probe, identity_id = await _setup(
        hub_under_test, space, f"rc_keep_{lifecycle}_{standing}", lifecycle=lifecycle, standing=standing
    )

    result = await _revalidate(space, identity_id)

    assert result.credential_verdict is CredentialVerdict.VALID
    identity = await space.identities.get(identity_id)
    assert (identity.auth, identity.lifecycle, identity.standing) == (
        AuthState.AUTHENTICATED,
        lifecycle,
        standing,
    )


async def test_rejected_but_active_credential_is_rechecked(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    authenticator, _probe, identity_id = await _setup(hub_under_test, space, "rc_rejected_active")
    await space.secrets.reject_credential(
        identity_id, "default", name="default", code="credentials_rejected", actor="t"
    )

    result = await _revalidate(space, identity_id)

    assert result.credential_verdict is CredentialVerdict.VALID and authenticator.calls == 1


@pytest.mark.parametrize("state", ["missing", "disabled", "revoked", "expired"])
async def test_unusable_credentials_block_without_provider_call(
    hub_under_test: IdentityHub, space: SpaceHandle, state: str
) -> None:
    authenticator, _probe, identity_id = await _setup(hub_under_test, space, f"rc_block_{state}")
    key = KEY
    if state == "missing":
        key = ("default", "absent")
    elif state == "disabled":
        await space.secrets.disable_credential(identity_id, "default", actor="t")
    elif state == "revoked":
        await space.secrets.revoke_credential(identity_id, "default", actor="t")
    else:
        await space.secrets.put_credential(
            identity_id, "default", {"phone": "+7"}, expires_at=datetime(2025, 1, 1, tzinfo=UTC), actor="t"
        )

    result = await _revalidate(space, identity_id, key)

    assert result.status is RevalidationStatus.BLOCKED
    assert result.credential_verdict is CredentialVerdict.BLOCKED
    assert [blocker.code for blocker in result.blockers] == [f"credential_{state}"]
    assert authenticator.calls == 0


async def test_credentials_rejected_marks_exact_credential(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    _auth, _probe, identity_id = await _setup(hub_under_test, space, "rc_reject", mode="reject", session=True)

    result = await _revalidate(space, identity_id)

    assert result.status is RevalidationStatus.COMPLETED
    assert result.credential_verdict is CredentialVerdict.REJECTED and result.code == "credentials_rejected"
    assert result.retryable is False
    (credential,) = await space.secrets.credentials(identity_id)
    assert credential.last_rejected_at is not None
    identity = await space.identities.get(identity_id)
    assert identity.auth in {AuthState.NO_SESSION, AuthState.SESSION_INVALID}


async def test_technical_failure_keeps_session_invalid(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    _auth, _probe, identity_id = await _setup(
        hub_under_test, space, "rc_tech", mode="tech", session=True, standing="banned"
    )

    result = await _revalidate(space, identity_id)

    assert result.status is RevalidationStatus.FAILED and result.retryable is True
    assert result.credential_verdict is CredentialVerdict.FAILED
    identity = await space.identities.get(identity_id)
    assert (identity.auth, identity.standing) == (AuthState.SESSION_INVALID, Standing.BANNED)
    operations = {operation.kind: operation for operation in await _operations(space, identity_id)}
    assert operations["login"].state == "failed" and SECRET not in repr(operations["login"].result)
    assert SECRET not in repr(result)


async def test_banned_fault_is_applied_and_reported(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    _auth, _probe, identity_id = await _setup(hub_under_test, space, "rc_banned", mode="banned")

    result = await _revalidate(space, identity_id)

    assert result.credential_verdict is CredentialVerdict.INCONCLUSIVE and result.code == "identity_banned"
    assert (await space.identities.get(identity_id)).standing == Standing.BANNED


async def test_active_challenge_is_returned_without_provider_call(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    authenticator, _probe, identity_id = await _setup(
        hub_under_test, space, "rc_active_challenge", mode="challenge"
    )
    first = await _revalidate(space, identity_id)
    assert first.credential_verdict is CredentialVerdict.CHALLENGE and first.challenge_id is not None
    assert authenticator.calls == 1

    again = await _revalidate(space, identity_id, request_id="r-2")

    assert again.credential_verdict is CredentialVerdict.CHALLENGE
    assert again.challenge_id == first.challenge_id
    assert authenticator.calls == 1  # новый вопрос не открывается, провайдер не вызывается
    assert (await space.identities.get(identity_id)).auth == AuthState.CHALLENGE


async def test_provider_challenge_opens_one_without_logging_in(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    _auth, _probe, identity_id = await _setup(hub_under_test, space, "rc_challenge", mode="challenge")

    result = await _revalidate(space, identity_id)

    assert result.status is RevalidationStatus.COMPLETED and result.applied["auth"] == "applied"
    async with space._tx(own=True) as tx:
        opened = await tx.open_challenge_of(identity_id)
    assert opened is not None and result.challenge_id == opened.id


async def test_no_secrets_in_result_or_events(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    _auth, _probe, identity_id = await _setup(hub_under_test, space, "rc_secret")

    result = await _revalidate(space, identity_id)
    events = await space.admin.event_page(
        filters=EventFilters(identity_id=identity_id, include_journal=True), limit=100
    )

    assert SECRET not in repr(result) and SECRET not in repr(events.items)
