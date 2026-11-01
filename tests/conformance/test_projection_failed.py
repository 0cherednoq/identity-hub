"""AR1.1: отказ проекции не оставляет операцию провайдера в `running` (D-335, L1).

Провайдерная операция и проекция результата пишутся одной транзакцией; исключение проекции откатывало
обе записи. Теперь операция терминализуется отдельной транзакцией как `failed/projection_failed`
с безопасным вердиктом в `result`, исключение пробрасывается прежним.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel, SecretStr
from tests.fixtures.services import FAKE_TG, TgSession

import identity_hub.auth as auth_module
import identity_hub.sync as sync_module
from identity_hub import (
    AuthState,
    IdentityCreate,
    IdentityHub,
    LoginChallenge,
    LoginOutcome,
    ProbeContext,
    ProbeResult,
    Service,
    SessionAdoptionOutcome,
    SessionVerificationOutcome,
    SpaceHandle,
)
from identity_hub.domain.regions import Command
from identity_hub.errors import TransitionNotAllowed

pytestmark = [pytest.mark.asyncio, pytest.mark.conformance]

SECRET = "super-secret-session-token"


class Session(BaseModel):
    token: str


class AuthProbe:
    def __init__(self, result: ProbeResult) -> None:
        self.result = result
        self.calls = 0

    async def auth(self, ctx: ProbeContext) -> ProbeResult | None:
        self.calls += 1
        return self.result


class Authenticator:
    def __init__(self, *, challenge: bool = False) -> None:
        self.challenge = challenge
        self.calls = 0

    async def login(self, ctx: Any) -> BaseModel:
        self.calls += 1
        if self.challenge and ctx.challenge_answer is None:
            raise LoginChallenge("sms_code", "Код из SMS", {}, timedelta(minutes=5))
        return TgSession(auth_key=SecretStr(SECRET))


def _boom(*_args: Any, **_kwargs: Any) -> None:
    raise TransitionNotAllowed("auth", "challenge", "authenticated")


def _fail_when(predicate: Any, original: Any) -> Any:
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        change = args[5] if len(args) > 5 else kwargs.get("change")
        if predicate(args[3] if len(args) > 3 else None, change):
            _boom()
        return await original(*args, **kwargs)

    return wrapper


async def _operations(space: SpaceHandle, identity_id: UUID) -> list[Any]:
    async with space._tx(own=True) as tx:
        return await tx.list_provider_operations(identity_id=identity_id, states=None, limit=20)


async def _probe_identity(
    hub: IdentityHub, space: SpaceHandle, name: str, verdict: AuthState
) -> tuple[AuthProbe, UUID]:
    probe = AuthProbe(ProbeResult(verdict))
    hub.register_service(Service(name, name, session=Session, probes=probe))
    await hub.start()
    created = await space.identities.create(
        IdentityCreate(service=name, external_ref=f"{name}-1", lifecycle="provisioning", session={"token": SECRET}),
        actor="test",
    )
    return probe, created.id


def _assert_projection_failed(operation: Any, verdict: str) -> None:
    assert operation.state == "failed"
    assert operation.error_code == "projection_failed"
    assert operation.result["projection"] == "failed"
    assert operation.result["outcome"] == verdict
    assert SECRET not in repr(operation.result)


@pytest.mark.parametrize("method", ["verify_session", "adopt_session"])
async def test_targeted_session_check_projection_failure_terminalizes_operation(
    hub_under_test: IdentityHub, space: SpaceHandle, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    probe, identity_id = await _probe_identity(
        hub_under_test, space, f"pf_{method}", AuthState.SESSION_INVALID
    )
    monkeypatch.setattr(auth_module, "apply_region_change", _fail_when(lambda *_: True, auth_module.apply_region_change))

    with pytest.raises(TransitionNotAllowed):
        await getattr(space.auth, method)(identity_id, actor="checker")

    (operation,) = await _operations(space, identity_id)
    _assert_projection_failed(operation, "invalid")
    monkeypatch.undo()
    # повтор не упирается в «проверка уже выполняется»
    again = await getattr(space.auth, method)(identity_id, actor="checker")
    assert again.outcome in {SessionVerificationOutcome.INVALID, SessionAdoptionOutcome.INVALID}
    assert probe.calls == 2


async def test_probe_projection_failure_terminalizes_operation(
    hub_under_test: IdentityHub, space: SpaceHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe, identity_id = await _probe_identity(hub_under_test, space, "pf_probe", AuthState.SESSION_INVALID)
    monkeypatch.setattr(sync_module, "apply_region_change", _fail_when(lambda *_: True, sync_module.apply_region_change))

    with pytest.raises(TransitionNotAllowed):
        await space.sync.refresh(identity_id, regions=("auth",))

    (operation,) = [op for op in await _operations(space, identity_id) if op.kind == "probe:auth"]
    _assert_projection_failed(operation, "session_invalid")
    monkeypatch.undo()
    refreshed = await space.sync.refresh(identity_id, regions=("auth",))
    assert refreshed.outcomes == {"auth": "applied"}
    assert probe.calls == 2


async def test_login_projection_failure_terminalizes_operation_and_restores_auth(
    hub_under_test: IdentityHub, space: SpaceHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    authenticator = Authenticator()
    hub_under_test.register_service(replace(FAKE_TG, name="pf_login", authenticator=authenticator))
    await hub_under_test.start()
    created = await space.identities.create(
        IdentityCreate(service="pf_login", external_ref="pf1", credentials={"phone": "pf1"}), actor="test"
    )
    original = auth_module.apply_region_change
    monkeypatch.setattr(
        auth_module,
        "apply_region_change",
        _fail_when(
            lambda _region, change: isinstance(change, Command) and change.to == AuthState.AUTHENTICATED,
            original,
        ),
    )

    assert await space.auth.login(created.id) is LoginOutcome.FAILED

    (operation,) = await _operations(space, created.id)
    _assert_projection_failed(operation, "succeeded")
    assert (await space.identities.get(created.id)).auth == AuthState.NO_SESSION
    monkeypatch.undo()
    assert await space.auth.login(created.id) is LoginOutcome.SUCCEEDED  # повтор не блокируется
    assert authenticator.calls == 2


async def test_challenge_projection_failure_terminalizes_operation(
    hub_under_test: IdentityHub, space: SpaceHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_under_test.register_service(
        replace(FAKE_TG, name="pf_challenge", authenticator=Authenticator(challenge=True))
    )
    await hub_under_test.start()
    created = await space.identities.create(
        IdentityCreate(service="pf_challenge", external_ref="pf2", credentials={"phone": "pf2"}), actor="test"
    )
    original = auth_module.apply_region_change
    monkeypatch.setattr(
        auth_module,
        "apply_region_change",
        _fail_when(
            lambda _region, change: isinstance(change, Command) and change.to == AuthState.CHALLENGE,
            original,
        ),
    )

    assert await space.auth.login(created.id) is LoginOutcome.FAILED

    (operation,) = await _operations(space, created.id)
    _assert_projection_failed(operation, "challenged")
