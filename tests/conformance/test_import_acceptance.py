"""S7: imported identities are accepted by a durable worker policy."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import BaseModel

from identity_hub import (
    AcceptPolicy,
    IdentityHub,
    ImportRecord,
    LoginChallenge,
    LoginFailure,
    Service,
    SpaceHandle,
    StaticSecretsProvider,
)
from identity_hub.clock import FakeClock
from identity_hub.read.events import EventFilters
from identity_hub.testing import MemoryStore
from identity_hub.testing.memory import MemoryTx

pytestmark = pytest.mark.conformance


class Session(BaseModel):
    token: str


class ScriptedAuthenticator:
    def __init__(self) -> None:
        self.calls: defaultdict[str, int] = defaultdict(int)

    async def login(self, ctx: object) -> Session:
        ref = str(ctx.external_ref)
        self.calls[ref] += 1
        if ref == "temporary" and self.calls[ref] == 1:
            raise LoginFailure("temporary", retryable=True)
        if ref in {"bad", "never-again"}:
            raise LoginFailure("credentials_rejected", retryable=False)
        if ref == "banned":
            raise LoginFailure("identity_banned", retryable=False)
        if ref == "challenged":
            raise LoginChallenge("sms", "Enter the SMS code", {}, timedelta(minutes=5))
        return Session(token=f"session-{ref}")


async def _pending(space: SpaceHandle) -> dict[UUID, tuple[dict[str, object], int]]:
    async with space._tx(own=True) as tx:
        return {row.identity_id: (row.policy, row.attempts) for row in await tx.list_acceptance()}


async def test_import_policy_is_queued_and_verifies_sessions_by_default(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    policy = AcceptPolicy(activate_on_login=True, max_attempts=2)
    service = Service(
        name="accept_queue",
        title="Accept queue",
        session=Session,
        default_accept=policy,
    )
    hub_under_test.register_service(service)
    await hub_under_test.start()

    report = await space.imports.run(
        service=service,
        records=[ImportRecord("queued", sessions={"default": {"token": "unverified"}})],
        actor="test",
    )
    identity = await space.identities.get(report.created[0])
    assert identity.auth == "no_session"
    assert await _pending(space) == {identity.id: (policy.dump(), 0)}

    plain = Service(name="accept_none", title="No acceptance")
    hub_under_test.register_service(plain)
    await hub_under_test.start()
    untouched = await space.imports.run(
        service=plain,
        records=[ImportRecord("manual")],
        actor="test",
    )
    assert untouched.created[0] not in await _pending(space)


async def test_acceptance_worker_applies_success_retire_retry_and_nonretryable(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    authenticator = ScriptedAuthenticator()
    policy = AcceptPolicy(
        activate_on_login=True,
        retire_on=frozenset({"credentials_rejected", "identity_banned"}),
        max_attempts=2,
    )
    service = Service(
        name="accept_worker",
        title="Acceptance worker",
        session=Session,
        authenticator=authenticator,
    )
    hub_under_test.register_service(service)
    await hub_under_test.start()
    report = await space.imports.run(
        service=service,
        records=[ImportRecord(ref) for ref in ("valid", "bad", "banned", "temporary", "challenged")],
        accept=policy,
        actor="test",
    )
    by_ref = {
        identity.external_ref: identity.id
        for identity in [await space.identities.get(identity_id) for identity_id in report.created]
    }

    assert (await hub_under_test.workers.acceptance.run_once(limit=10)).processed == 5
    assert (await space.identities.get(by_ref["valid"])).lifecycle == "ready"
    assert (await space.identities.get(by_ref["bad"])).lifecycle == "retired"
    assert (await space.identities.get(by_ref["banned"])).lifecycle == "retired"
    assert (await space.identities.get(by_ref["temporary"])).lifecycle == "provisioning"
    challenged = await space.identities.get(by_ref["challenged"])
    assert challenged.lifecycle == "provisioning" and challenged.auth == "challenge"
    assert (await _pending(space))[by_ref["temporary"]][1] == 1

    assert (await hub_under_test.workers.acceptance.run_once(limit=10)).processed == 1
    assert (await space.identities.get(by_ref["temporary"])).lifecycle == "ready"
    assert not await _pending(space)
    events = await space.admin.event_page(
        filters=EventFilters(type="identity.lifecycle.changed", include_journal=True),
        limit=100,
    )
    assert any(event.identity_id == by_ref["valid"] and event.data["to"] == "ready" for event in events.items)

    no_retire = AcceptPolicy(max_attempts=3)
    second = await space.imports.run(
        service=service,
        records=[ImportRecord("never-again")],
        accept=no_retire,
        actor="test",
    )
    assert (await hub_under_test.workers.acceptance.run_once(limit=10)).processed == 1
    calls = authenticator.calls["never-again"]
    await space.auth.login_many(service=service, limit=10)
    assert authenticator.calls["never-again"] == calls
    async with space._tx(own=True) as tx:
        auth = await tx.load_region(second.created[0], "auth", "default")
    assert auth is not None and auth.retryable is False
    await space.secrets.replace_credentials(second.created[0], {}, actor="test", reset_auth=True)
    await space.auth.login_many(service=service, limit=10)
    assert authenticator.calls["never-again"] == calls + 1


async def test_maintenance_runs_acceptance_worker(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    service = Service(
        name="accept_maintenance",
        title="Acceptance maintenance",
        session=Session,
        authenticator=ScriptedAuthenticator(),
    )
    hub_under_test.register_service(service)
    await hub_under_test.start()
    report = await space.imports.run(
        service=service,
        records=[ImportRecord("maintenance-valid")],
        accept=AcceptPolicy(activate_on_login=True),
        actor="test",
    )
    maintenance = await hub_under_test.maintenance.run_all()
    assert maintenance.counts["acceptance"] == 1
    assert (await space.identities.get(report.created[0])).lifecycle == "ready"


@pytest.mark.parametrize("size", [10, 1000])
async def test_acceptance_enqueue_is_one_bulk_call_per_import_batch(
    monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    calls: list[int] = []
    original = MemoryTx.enqueue_acceptance

    async def counted(self: MemoryTx, identity_ids: list[UUID], policy: dict[str, object]) -> int:
        calls.append(len(identity_ids))
        return await original(self, identity_ids, policy)

    monkeypatch.setattr(MemoryTx, "enqueue_acceptance", counted)
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    store = MemoryStore(clock)
    service = Service(name=f"accept_bulk_{size}", title="Bulk")
    hub = IdentityHub(
        store=store,
        secrets=StaticSecretsProvider({"k": b"1" * 32}, "k"),
        services=[service],
        clock=clock,
    )
    await hub.start()
    space = hub.space(f"bulk-{size}", create=True)
    report = await space.imports.run(
        service=service,
        records=[ImportRecord(f"user-{index}") for index in range(size)],
        batch_size=size,
        accept=AcceptPolicy(),
        actor="test",
    )
    assert len(report.created) == size
    assert calls == [size]
