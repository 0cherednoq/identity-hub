"""IMP-11 (I4.3): пробы через очередь операций и восстановление истёкших claim (D-223)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from tests.fixtures.services import FAKE_TG

from identity_hub import IdentityCreate, IdentityHub, ProbeResult, SpaceHandle

pytestmark = [pytest.mark.anyio, pytest.mark.conformance]


class GatedProbes:
    def __init__(self) -> None:
        self.gate: asyncio.Event | None = None
        self.called = asyncio.Event()
        self.fail = False

    async def auth(self, ctx: Any) -> ProbeResult | None:
        self.called.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise ConnectionError("platform down")
        return ProbeResult("authenticated")

    async def standing(self, ctx: Any) -> ProbeResult | None:
        return ProbeResult("limited")

    async def verification(self, ctx: Any, factor: str) -> ProbeResult | None:
        return None


async def _setup(hub: IdentityHub, space: SpaceHandle, name: str) -> tuple[GatedProbes, UUID]:
    probes = GatedProbes()
    hub.register_service(replace(FAKE_TG, name=name, probes=probes))
    await hub.start()
    created = await space.identities.create(
        IdentityCreate(service=name, external_ref=f"{name}-1", lifecycle="ready", session={"auth_key": "k"}),
        actor="test",
    )
    return probes, created.id


async def _operations(space: SpaceHandle, identity_id: UUID) -> dict[str, Any]:
    async with space._tx(own=True) as tx:
        rows = await tx.list_provider_operations(identity_id=identity_id, states=None, limit=50)
    return {row.kind: row for row in rows}


async def test_probe_goes_through_operation_queue(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    _probes, identity_id = await _setup(hub_under_test, space, "pr_ok")

    outcomes = await space.sync.refresh(identity_id, ["auth", "standing"])

    assert set(outcomes.outcomes) == {"auth", "standing"}
    operations = await _operations(space, identity_id)
    assert {kind: row.state for kind, row in operations.items()} == {
        "probe:auth": "succeeded",
        "probe:standing": "succeeded",
    }
    assert (await space.identities.get(identity_id)).standing == "limited"


async def test_probe_failure_is_operation_outcome(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    probes, identity_id = await _setup(hub_under_test, space, "pr_fail")
    probes.fail = True

    with pytest.raises(ConnectionError):
        await space.sync.refresh(identity_id, ["auth"])

    operation = (await _operations(space, identity_id))["probe:auth"]
    assert (operation.state, operation.error_code) == ("failed", "ConnectionError")


async def test_live_operation_makes_region_busy(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    _probes, identity_id = await _setup(hub_under_test, space, "pr_busy")
    async with space._tx(own=True) as tx:
        await tx.enqueue_provider_operation(identity_id, "probe:auth")  # чужая живая операция

    outcomes = await space.sync.refresh(identity_id, ["auth", "standing"])

    assert outcomes.outcomes["auth"] == "busy" and outcomes.outcomes["standing"] != "busy"


async def test_late_probe_result_is_rejected_and_recovery_leaves_no_expired_running(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    probes, identity_id = await _setup(hub_under_test, space, "pr_late")
    probes.gate = asyncio.Event()
    before = (await space.identities.get(identity_id)).auth
    refresh = asyncio.create_task(space.sync.refresh(identity_id, ["auth"]))
    await probes.called.wait()  # claim закоммичен, проба вызвана
    running = (await _operations(space, identity_id))["probe:auth"]
    await _expire_claim(hub_under_test, space, running.id)

    assert await hub_under_test._provider_worker.run_once(limit=10) == 1
    probes.gate.set()
    with pytest.raises(Exception):  # noqa: B017 — поздний ответ отклоняется, тип зависит от пути
        await refresh

    after = (await _operations(space, identity_id))["probe:auth"]
    assert after.state == "failed" and after.error_code == "claim_expired"
    assert (await space.identities.get(identity_id)).auth == before
    async with space._tx(own=True) as tx:
        live = await tx.list_provider_operations(
            identity_id=identity_id, states=frozenset({"running"}), limit=10
        )
    assert live == []  # после восстановления нет running с истёкшим claim


async def _expire_claim(hub: IdentityHub, space: SpaceHandle, operation_id: UUID) -> None:
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
