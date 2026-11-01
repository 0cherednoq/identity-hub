"""AR1.2: восстановленная операция явно помечена как неизвестный исход (D-335, L1).

Падение между ответом провайдера и коммитом оставляет операцию `running`; recovery переводит её в
`failed/claim_expired`. Вердикт провайдера при этом потерян, поэтому в безопасном `result` фиксируется
`outcome=unknown`: это «не знаем, что произошло», а не «ничего не произошло».
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel, SecretStr
from tests.conformance.test_session_first_auth import _expire_provider_claim
from tests.fixtures.services import FAKE_TG, TgSession

from identity_hub import (
    AuthState,
    IdentityCreate,
    IdentityHub,
    LoginOutcome,
    ProbeContext,
    ProbeResult,
    Service,
    SpaceHandle,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.conformance]


class Session(BaseModel):
    token: str


class GatedProbe:
    def __init__(self) -> None:
        self.called = asyncio.Event()
        self.gate = asyncio.Event()
        self.calls = 0

    async def auth(self, ctx: ProbeContext) -> ProbeResult | None:
        self.calls += 1
        self.called.set()
        await self.gate.wait()
        return ProbeResult(AuthState.AUTHENTICATED)


class GatedAuthenticator:
    def __init__(self) -> None:
        self.called = asyncio.Event()
        self.gate = asyncio.Event()
        self.calls = 0

    async def login(self, ctx: Any) -> BaseModel:
        self.calls += 1
        self.called.set()
        await self.gate.wait()
        return TgSession(auth_key=SecretStr("key"))


async def _operations(space: SpaceHandle, identity_id: UUID) -> list[Any]:
    async with space._tx(own=True) as tx:
        return await tx.list_provider_operations(identity_id=identity_id, states=None, limit=20)


async def test_probe_recovered_after_claim_expiry_is_marked_unknown_and_retry_is_not_blocked(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    probe = GatedProbe()
    hub_under_test.register_service(Service("rec_probe", "rec_probe", session=Session, probes=probe))
    await hub_under_test.start()
    created = await space.identities.create(
        IdentityCreate(
            service="rec_probe", external_ref="r1", lifecycle="provisioning", session={"token": "t"}
        ),
        actor="test",
    )
    refresh = asyncio.create_task(space.sync.refresh(created.id, regions=("auth",)))
    await probe.called.wait()  # claim закоммичен, адаптер вызван
    (running,) = await _operations(space, created.id)
    await _expire_provider_claim(hub_under_test, space, running.id)

    run = await hub_under_test.workers.provider_operations.run_once()

    assert run.counts == {"recovered": 1}
    (recovered,) = await _operations(space, created.id)
    assert (recovered.state, recovered.error_code) == ("failed", "claim_expired")
    assert recovered.result == {"outcome": "unknown"}
    probe.gate.set()
    with pytest.raises(Exception):  # noqa: B017 — поздний ответ отклоняется, тип зависит от пути
        await refresh
    assert (await space.sync.refresh(created.id, regions=("auth",))).outcomes == {"auth": "unchanged"}
    assert probe.calls == 2


async def test_login_recovered_after_claim_expiry_is_marked_unknown(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    authenticator = GatedAuthenticator()
    hub_under_test.register_service(replace(FAKE_TG, name="rec_login", authenticator=authenticator))
    await hub_under_test.start()
    created = await space.identities.create(
        IdentityCreate(service="rec_login", external_ref="r2", credentials={"phone": "r2"}), actor="test"
    )
    login = asyncio.create_task(space.auth.login(created.id))
    await authenticator.called.wait()
    (running,) = await _operations(space, created.id)
    await _expire_provider_claim(hub_under_test, space, running.id)

    run = await hub_under_test.workers.provider_operations.run_once()

    assert run.counts == {"recovered": 1}
    (recovered,) = await _operations(space, created.id)
    assert (recovered.state, recovered.error_code, recovered.result) == (
        "failed",
        "claim_expired",
        {"outcome": "unknown"},
    )
    authenticator.gate.set()
    assert await login is LoginOutcome.FAILED  # поздний результат старого воркера ничего не пишет


async def test_recovery_worker_reports_zero_when_nothing_expired(hub_under_test: IdentityHub) -> None:
    run = await hub_under_test.workers.provider_operations.run_once()
    assert run.counts == {"recovered": 0}
    assert run.processed == 0
