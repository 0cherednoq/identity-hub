"""IMP-11 (I4.2): вход идёт через очередь операций провайдера (D-222)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel, SecretStr
from tests.fixtures.services import FAKE_TG, TgSession

from identity_hub import IdentityCreate, IdentityHub, LoginChallenge, LoginOutcome, SpaceHandle

pytestmark = [pytest.mark.anyio, pytest.mark.conformance]


class RecordingAuthenticator:
    """Запоминает attempt_id; по внешнему ref выбирает исход."""

    def __init__(self) -> None:
        self.attempts: list[UUID | None] = []
        self.gate: asyncio.Event | None = None
        self.called = asyncio.Event()

    async def login(self, ctx: Any) -> BaseModel:
        self.attempts.append(ctx.attempt_id)
        self.called.set()
        if self.gate is not None:
            await self.gate.wait()
        if ctx.external_ref.startswith("fail"):
            raise ConnectionError("platform down")
        if ctx.external_ref.startswith("ask") and ctx.challenge_answer is None:
            raise LoginChallenge("sms_code", "Код из SMS", {}, timedelta(minutes=5))
        return TgSession(auth_key=SecretStr(f"key-{ctx.external_ref}"))


async def _setup(hub: IdentityHub, space: SpaceHandle, ref: str) -> tuple[RecordingAuthenticator, UUID]:
    auth = RecordingAuthenticator()
    hub.register_service(replace(FAKE_TG, name=f"op_{ref}", authenticator=auth))
    await hub.start()
    created = await space.identities.create(
        IdentityCreate(service=f"op_{ref}", external_ref=ref, credentials={"phone": ref}), actor="test"
    )
    return auth, created.id


async def _operations(space: SpaceHandle, identity_id: UUID) -> list[Any]:
    async with space._tx(own=True) as tx:
        return await tx.list_provider_operations(identity_id=identity_id, states=None, limit=20)


async def test_login_goes_through_operation_queue(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    auth, identity_id = await _setup(hub_under_test, space, "ok1")

    assert await space.auth.login(identity_id) is LoginOutcome.SUCCEEDED

    (operation,) = await _operations(space, identity_id)
    assert (operation.kind, operation.state, operation.attempt) == ("login", "succeeded", 1)
    assert auth.attempts == [operation.claim_id]  # ключ идемпотентности для платформы
    assert (await space.identities.get(identity_id)).auth == "authenticated"


async def test_failure_and_challenge_are_operation_outcomes(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    _auth, failing = await _setup(hub_under_test, space, "fail1")
    _auth2, asking = await _setup(hub_under_test, space, "ask1")

    assert await space.auth.login(failing) is LoginOutcome.FAILED
    assert await space.auth.login(asking) is LoginOutcome.CHALLENGED

    (failed,) = await _operations(space, failing)
    assert (failed.state, failed.error_code) == ("failed", "ConnectionError")
    (challenged,) = await _operations(space, asking)
    assert (challenged.state, challenged.result) == ("succeeded", {"outcome": "challenged"})
    report = await space.auth.login_many(service="op_fail1")  # аккаунт снова в no_session
    retried = [op for op in await _operations(space, failing) if op.id != failed.id]
    assert report.operations == {failing: retried[0].id}  # отчёт называет операцию попытки


async def test_late_result_of_old_worker_writes_nothing(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    """Воркер завис после claim; claim истёк и операцию восстановили — его поздний ответ откатывается."""
    auth, identity_id = await _setup(hub_under_test, space, "late1")
    auth.gate = asyncio.Event()
    login = asyncio.create_task(space.auth.login(identity_id))
    await auth.called.wait()  # claim закоммичен, адаптер вызван
    (running,) = await _operations(space, identity_id)
    await _expire_claim(hub_under_test, space, running.id)
    async with space._tx(own=True) as tx:
        assert await tx.recover_provider_operations(limit=10) == 1
    auth.gate.set()

    assert await login is LoginOutcome.FAILED
    (after,) = await _operations(space, identity_id)
    assert after.state == "retryable"  # результат старого воркера не применён
    assert (await space.identities.get(identity_id)).auth != "authenticated"
    async with space._tx(own=True) as tx:
        assert await tx.get_session(identity_id, "default") is None


async def _expire_claim(hub: IdentityHub, space: SpaceHandle, operation_id: UUID) -> None:
    """Срок claim в прошлом — как если бы воркер завис дольше `claim_for`."""
    from identity_hub.testing import MemoryStore

    store = hub._store
    if isinstance(store, MemoryStore):
        state = store._state
        row = state.provider_operations[operation_id]
        state.provider_operations[operation_id] = replace(
            row, claim_expires_at=hub.clock.now() - timedelta(seconds=1)
        )
        return
    from sqlalchemy import text

    async with space._tx(own=True) as tx:
        await tx.execute(  # type: ignore[attr-defined]
            text(
                "UPDATE ih_provider_operations SET claim_expires_at = now() - interval '1 second' "
                "WHERE id = :id"
            ),
            {"id": operation_id},
        )
