"""AR0.1: вердикт провайдера применяется как Observation, а не как Command (D-335).

Регрессия на инцидент: `TransitionNotAllowed` после вызова провайдера откатывал терминализацию
операции, и она оставалась `running`.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel

from identity_hub import (
    AuthState,
    IdentityCreate,
    IdentityHub,
    Override,
    ProbeContext,
    ProbeResult,
    Service,
    SessionAdoptionOutcome,
    SessionVerificationOutcome,
    SpaceHandle,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.conformance]

S = AuthState
# состояние auth после вердикта session_invalid из каждого исходного состояния
EXPECTED_AFTER = {
    S.NO_SESSION: S.SESSION_INVALID,
    S.AUTHENTICATED: S.SESSION_INVALID,
    S.SESSION_INVALID: S.SESSION_INVALID,
    S.CHALLENGE: S.CHALLENGE,  # интерактивный вход не затирается наблюдением
    S.LOGGING_IN: S.LOGGING_IN,
}


class Session(BaseModel):
    token: str


class AuthProbe:
    def __init__(self, result: ProbeResult) -> None:
        self.result = result
        self.calls = 0

    async def auth(self, ctx: ProbeContext) -> ProbeResult | None:
        self.calls += 1
        return self.result


async def _setup(hub: IdentityHub, space: SpaceHandle, name: str, state: AuthState) -> tuple[AuthProbe, UUID]:
    probe = AuthProbe(ProbeResult(AuthState.SESSION_INVALID))
    hub.register_service(Service(name, name, session=Session, probes=probe))
    await hub.start()
    created = await space.identities.create(
        IdentityCreate(
            service=name, external_ref=f"{name}-1", lifecycle="provisioning", session={"token": "t"}
        ),
        actor="test",
    )
    # create(session=...) сразу даёт authenticated: исходное состояние ставим явно
    if (await space.identities.get(created.id)).auth != state:
        await space.identities.override(created.id, Override("auth", "", state, reason="setup", actor="test"))
    return probe, created.id


async def _operation_states(hub: IdentityHub, identity_id: UUID) -> list[str]:
    store: Any = hub._store
    if hasattr(store, "_state"):
        return [
            f"{row.kind}:{row.state}"
            for row in store._state.provider_operations.values()
            if row.identity_id == identity_id
        ]
    from sqlalchemy import text

    async with store.sessionmaker() as session:
        result = await session.execute(
            text("SELECT kind, state FROM ih_provider_operations WHERE identity_id = :id"),
            {"id": identity_id},
        )
        return [f"{kind}:{state}" for kind, state in result.all()]


@pytest.mark.parametrize("method", ["verify_session", "adopt_session"])
@pytest.mark.parametrize("initial", list(EXPECTED_AFTER), ids=lambda state: state.value)
async def test_session_invalid_verdict_is_terminal_from_every_auth_state(
    hub_under_test: IdentityHub, space: SpaceHandle, method: str, initial: AuthState
) -> None:
    probe, identity_id = await _setup(hub_under_test, space, f"ar_{method}_{initial.value}", initial)

    result = await getattr(space.auth, method)(identity_id, actor="checker")

    assert result.outcome in {SessionVerificationOutcome.INVALID, SessionAdoptionOutcome.INVALID}
    assert (await space.identities.get(identity_id)).auth == EXPECTED_AFTER[initial]
    assert await _operation_states(hub_under_test, identity_id) == ["probe:auth:succeeded"]
    assert probe.calls == 1
    # повтор не упирается в зависшую операцию
    again = await getattr(space.auth, method)(identity_id, actor="checker")
    assert again.outcome in {SessionVerificationOutcome.INVALID, SessionAdoptionOutcome.INVALID}
    assert probe.calls == 2


@pytest.mark.parametrize("initial", [AuthState.CHALLENGE, AuthState.LOGGING_IN], ids=lambda s: s.value)
async def test_probe_does_not_overwrite_interactive_login(
    hub_under_test: IdentityHub, space: SpaceHandle, initial: AuthState
) -> None:
    _probe, identity_id = await _setup(hub_under_test, space, f"ar_probe_{initial.value}", initial)

    refreshed = await space.sync.refresh(identity_id, regions=("auth",))

    assert refreshed.outcomes == {"auth": "sticky"}
    assert (await space.identities.get(identity_id)).auth == initial


async def test_probe_authenticated_closes_challenge(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    probe, identity_id = await _setup(hub_under_test, space, "ar_probe_closes_challenge", AuthState.CHALLENGE)
    probe.result = ProbeResult(AuthState.AUTHENTICATED)

    refreshed = await space.sync.refresh(identity_id, regions=("auth",))

    assert refreshed.outcomes == {"auth": "applied"}
    assert (await space.identities.get(identity_id)).auth == AuthState.AUTHENTICATED


async def test_pinned_auth_is_not_changed_by_verdict_but_operation_is_terminal(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    probe, identity_id = await _setup(hub_under_test, space, "ar_pinned", AuthState.AUTHENTICATED)
    await space.identities.override(
        identity_id,
        Override("auth", "", AuthState.AUTHENTICATED, reason="pin", actor="test", pin_for=timedelta(hours=1)),
    )

    result = await space.auth.verify_session(identity_id, actor="checker")

    assert result.outcome is SessionVerificationOutcome.INVALID
    assert (await space.identities.get(identity_id)).auth == AuthState.AUTHENTICATED
    assert await _operation_states(hub_under_test, identity_id) == ["probe:auth:succeeded"]
    assert probe.calls == 1


@pytest.mark.parametrize("method", ["verify_session", "adopt_session"])
async def test_session_invalid_verdict_keeps_retryable_false(
    hub_under_test: IdentityHub, space: SpaceHandle, method: str
) -> None:
    """Приёмка и `login_many` читают `retryable is False`: вердикт не должен стать «повторяемым»."""
    _probe, identity_id = await _setup(
        hub_under_test, space, f"ar_retryable_{method}", AuthState.AUTHENTICATED
    )

    await getattr(space.auth, method)(identity_id, actor="checker")

    card = await space.identities.card(identity_id)
    auth = next(region for region in card.regions if region.region == "auth")
    assert auth.state == AuthState.SESSION_INVALID
    assert auth.retryable is False
