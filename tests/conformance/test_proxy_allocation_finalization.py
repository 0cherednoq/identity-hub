"""PA3: встроенные проверки принимают verdict и назначение до внутреннего release."""

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest
from pydantic import BaseModel
from sqlalchemy import func, update

from identity_hub import (
    AllOf,
    AnyOf,
    AuthState,
    Blame,
    ChangeSource,
    IdentityCreate,
    ImportRecord,
    LeaseOwnershipLost,
    LeaseRevoked,
    LoginFailure,
    Observation,
    OperationRule,
    Override,
    ProbeResult,
    ProxyAssignmentConflict,
    ProxyBindingPolicy,
    ProxyPolicy,
    RegionIn,
    Service,
)
from identity_hub.clock import FakeClock
from identity_hub.postgres.models import ProviderOperationRecord
from identity_hub.protocols import Fault
from identity_hub.read.events import EventFilters

pytestmark = pytest.mark.conformance


class Session(BaseModel):
    token: str


class Adapter:
    def __init__(self):
        self.result = ProbeResult(AuthState.AUTHENTICATED)
        self.standing_result = ProbeResult("good")
        self.error = None
        self.entered = asyncio.Event()
        self.finish = None
        self.proxy = None
        self.fault = None
        self.login_failure = None

    def classify(self, exc, *, kind=None):
        return self.fault

    async def auth(self, ctx):
        self.proxy = ctx.proxy_url
        self.entered.set()
        if self.finish is not None:
            await self.finish.wait()
        if self.error is not None:
            raise self.error
        return self.result

    async def standing(self, ctx):
        return self.standing_result

    async def login(self, ctx):
        self.proxy = ctx.proxy_url
        if self.login_failure is not None:
            raise self.login_failure
        return Session(token="session-from-login")


async def _setup(hub, space, *, with_session=True, backup=False, lifecycle="ready", binding=None):
    proxy = await space.proxies.create("http://user:secret@127.0.0.1:8990", group="g", max_identities=1)
    await space.proxies.configure_group("g", capacity_accounting="allocations", actor="test", reason="enable")
    adapter = Adapter()
    rule = binding or ProxyBindingPolicy(
        bind_when=AllOf(RegionIn("auth", states={"authenticated"}), RegionIn("lifecycle", states={"ready"})),
        unbind_when=AnyOf(
            RegionIn("auth", states={"session_invalid"}), RegionIn("standing", states={"banned"})
        ),
    )
    service = Service(
        "proxy_checks",
        "Proxy checks",
        session=Session,
        probes=adapter,
        authenticator=adapter,
        operations={"work": OperationRule()},
        error_classifier=adapter,
        proxy_policy=ProxyPolicy(group="g", binding=rule),
    )
    hub.register_service(service)
    await hub.start()
    identity = await space.identities.create(
        IdentityCreate(
            service=service.name,
            external_ref="a",
            lifecycle=lifecycle,
            session={"token": "existing"} if with_session else None,
            sessions={"backup": {"token": "backup"}} if backup else {},
        ),
        actor="test",
    )
    return proxy, identity.id, adapter, service


async def _binding(space, identity):
    async with space._tx() as tx:
        return await tx.proxy_bindings(identity_id=identity)


async def _operations(space, identity):
    async with space._tx() as tx:
        return await tx.list_provider_operations(identity_id=identity, states=None, limit=20)


@pytest.mark.parametrize("method", ["verify_session", "adopt_session", "revalidate", "sync", "login"])
async def test_builtin_result_confirms_before_internal_release(hub_under_test, space, method):
    proxy, identity, adapter, _ = await _setup(hub_under_test, space, with_session=method != "login")
    if method == "revalidate":
        await space.auth.revalidate(identity, actor="checker", reason="check", request_id="job")
    elif method == "sync":
        await space.sync.refresh(identity, regions=["auth"])
    elif method == "login":
        assert str(await space.auth.login(identity)) == "succeeded"
    else:
        await getattr(space.auth, method)(identity, actor="checker")
    (pair,) = await _binding(space, identity)
    assert pair.proxy_id == proxy.id
    assert "secret" in adapter.proxy
    async with space._tx() as tx:
        assert await tx.current_lease(identity) is None
        allocation = await tx.proxy_allocation(pair.allocation_id)
        assert allocation.state == "bound" and allocation.owner_lease_id is None
        row = await tx.get_proxy(proxy.id)
        assert (row.bound_count, row.reserved_count, row.draining_count) == (1, 0, 0)
    operations = await _operations(space, identity)
    assert operations and all(operation.state == "succeeded" for operation in operations)
    assert all(
        operation.result["proxy_assignment"]
        == {
            "outcome": "bound",
            "allocation_id": str(pair.allocation_id),
        }
        for operation in operations
    )


async def test_adoption_uses_final_auth_and_lifecycle_projection(hub_under_test, space):
    rule = ProxyBindingPolicy(
        bind_when=AllOf(RegionIn("auth", states={"authenticated"}), RegionIn("lifecycle", states={"ready"})),
        unbind_when=RegionIn("lifecycle", states={"provisioning"}),
    )
    _, identity, _, _ = await _setup(
        hub_under_test, space, with_session=False, backup=True, lifecycle="provisioning", binding=rule
    )
    await space.auth.adopt_session(identity, slot="backup", activate=True, actor="checker")
    assert len(await _binding(space, identity)) == 1
    row = await space.identities.get(identity)
    assert row.auth == "authenticated" and row.lifecycle == "ready"


async def test_acquire_does_not_confirm_preexisting_good_state(hub_under_test, space):
    _, identity, _, service = await _setup(hub_under_test, space)
    lease = await space.pools.define("work", service=service, holder="worker").acquire(among=[identity])
    assert lease.proxy_assignment_state == "reserved"
    assert await _binding(space, identity) == []
    await lease.release()


async def test_invalid_verdict_detaches_and_closes_draining_before_return(hub_under_test, space):
    proxy, identity, adapter, _ = await _setup(hub_under_test, space)
    await space.auth.verify_session(identity, actor="checker")
    (old,) = await _binding(space, identity)
    adapter.result = ProbeResult(AuthState.SESSION_INVALID)
    await space.auth.verify_session(identity, actor="checker")
    assert await _binding(space, identity) == []
    async with space._tx() as tx:
        assert (await tx.proxy_allocation(old.allocation_id)).state == "released"
        row = await tx.get_proxy(proxy.id)
        assert (row.bound_count, row.reserved_count, row.draining_count) == (0, 0, 0)
    assert any(
        op.result["proxy_assignment"]["outcome"] == "unbound" for op in await _operations(space, identity)
    )


async def test_pinned_auth_does_not_detach_from_raw_provider_verdict(hub_under_test, space):
    _, identity, adapter, _ = await _setup(hub_under_test, space)
    await space.auth.verify_session(identity, actor="checker")
    await space.identities.override(
        identity,
        Override("auth", None, "authenticated", actor="admin", reason="pin", pin_for=timedelta(hours=1)),
    )
    adapter.result = ProbeResult(AuthState.SESSION_INVALID)
    await space.auth.verify_session(identity, actor="checker")
    assert len(await _binding(space, identity)) == 1
    assert (await space.identities.get(identity)).auth == "authenticated"
    assert any(
        op.result["proxy_assignment"]["outcome"] == "not_accepted"
        for op in await _operations(space, identity)
    )


async def test_technical_probe_failure_does_not_detach(hub_under_test, space):
    _, identity, adapter, _ = await _setup(hub_under_test, space)
    await space.auth.verify_session(identity, actor="checker")
    adapter.error = RuntimeError("technical failure")
    await space.auth.verify_session(identity, actor="checker")
    assert len(await _binding(space, identity)) == 1


@pytest.mark.parametrize("source", ["override", "observe", "batch"])
async def test_state_command_unbinds_without_reservation_owner(hub_under_test, space, source):
    _, identity, _, _ = await _setup(hub_under_test, space)
    await space.auth.verify_session(identity, actor="checker")
    if source == "override":
        await space.identities.override(
            identity, Override("standing", None, "banned", actor="admin", reason="ban")
        )
    elif source == "observe":
        await space.identities.observe(
            identity, Observation("standing", None, "banned"), actor="checker", source=ChangeSource.PROBE
        )
    else:
        result = await space.identities.override_many(
            [identity], Override("standing", None, "banned", actor="admin", reason="ban")
        )
        assert result.applied == [identity]
    assert await _binding(space, identity) == []
    events = (await space.admin.event_page(filters=EventFilters(type="proxy.binding.changed"))).items
    assert len(events) == 2


async def test_late_verdict_keeps_admin_generation_and_records_conflict(hub_under_test, space):
    proxy, identity, adapter, _ = await _setup(hub_under_test, space)
    other = await space.proxies.create("http://127.0.0.1:8991", group="g", max_identities=1)
    adapter.finish = asyncio.Event()
    checking = asyncio.create_task(space.auth.verify_session(identity, actor="checker"))
    async with asyncio.timeout(10):
        await adapter.entered.wait()
        await space.proxies.rebind(identity, other.id, actor="admin", reason="replacement")
        adapter.finish.set()
        result = await checking
    assert str(result.outcome) == "valid"
    (pair,) = await _binding(space, identity)
    assert pair.proxy_id == other.id
    (operation,) = await _operations(space, identity)
    assert operation.state == "succeeded"
    assert operation.result["proxy_assignment"]["outcome"] == "assignment_changed"
    async with space._tx() as tx:
        row = await tx.get_proxy(proxy.id)
        assert (row.bound_count, row.reserved_count, row.draining_count) == (0, 0, 0)
        assert (await tx.get_proxy(other.id)).bound_count == 1


async def test_provider_commit_failure_rolls_back_confirmation_with_projection(
    hub_under_test, space, monkeypatch
):
    proxy, identity, _, _ = await _setup(hub_under_test, space)
    async with space._tx() as tx:
        backend = type(tx)
    original = backend.apply_provider_operation

    async def fail(self, claim, outcome, **kwargs):
        if outcome == "succeeded":
            # Finalizer уже превратил reserved в bound; эта запись и принятый
            # результат ещё принадлежат той же незавершённой транзакции.
            assert (await self.get_binding(identity)).id == proxy.id
            raise RuntimeError("after confirmation")
        return await original(self, claim, outcome, **kwargs)

    monkeypatch.setattr(backend, "apply_provider_operation", fail)
    with pytest.raises(RuntimeError, match="after confirmation"):
        await space.auth.verify_session(identity, actor="checker")
    assert await _binding(space, identity) == []
    async with space._tx() as tx:
        row = await tx.get_proxy(proxy.id)
        assert (row.bound_count, row.reserved_count, row.draining_count) == (0, 0, 0)
        _, sessions = await tx.access_metadata(identity)
        assert sessions[0].last_verified_at is None
    events = (await space.admin.event_page(filters=EventFilters(type="proxy.binding.changed"))).items
    assert not events
    (operation,) = await _operations(space, identity)
    assert operation.state == "failed" and operation.error_code == "projection_failed"


async def test_revoked_owner_cannot_accept_late_verdict(hub_under_test, space):
    proxy, identity, adapter, _ = await _setup(hub_under_test, space)
    adapter.finish = asyncio.Event()
    checking = asyncio.create_task(space.auth.verify_session(identity, actor="checker"))
    async with asyncio.timeout(10):
        await adapter.entered.wait()
        assert await space.pools.revoke(identity, actor="admin", reason="lost worker")
        adapter.finish.set()
        with pytest.raises((LeaseOwnershipLost, LeaseRevoked)):
            await checking
    assert await _binding(space, identity) == []
    async with space._tx() as tx:
        assert (await tx.get_proxy(proxy.id)).reserved_count == 0
        _, sessions = await tx.access_metadata(identity)
        assert sessions[0].last_verified_at is None


async def test_expired_claim_rolls_back_verdict_and_confirmation(hub_under_test, space, backend_sessionmaker):
    proxy, identity, adapter, _ = await _setup(hub_under_test, space)
    adapter.finish = asyncio.Event()
    checking = asyncio.create_task(space.auth.verify_session(identity, actor="checker"))
    async with asyncio.timeout(10):
        await adapter.entered.wait()
        (operation,) = await _operations(space, identity)
        if backend_sessionmaker is None:
            store = hub_under_test._store
            store._state.provider_operations[operation.id] = replace(
                store._state.provider_operations[operation.id],
                claim_expires_at=hub_under_test.clock.now() - timedelta(seconds=1),
            )
        else:
            async with backend_sessionmaker.begin() as session:
                await session.execute(
                    update(ProviderOperationRecord)
                    .where(ProviderOperationRecord.id == operation.id)
                    .values(claim_expires_at=func.now() - timedelta(seconds=1))
                )
        adapter.finish.set()
        with pytest.raises(LeaseOwnershipLost):
            await checking
    assert await _binding(space, identity) == []
    assert not (await space.admin.event_page(filters=EventFilters(type="proxy.reservation.confirmed"))).items
    async with space._tx() as tx:
        row = await tx.get_proxy(proxy.id)
        assert (row.bound_count, row.reserved_count, row.draining_count) == (0, 0, 0)
        _, sessions = await tx.access_metadata(identity)
        assert sessions[0].last_verified_at is None


async def test_negative_rule_wins_and_cancels_reservation(hub_under_test, space):
    rule = ProxyBindingPolicy(
        bind_when=RegionIn("auth", states={"authenticated"}),
        unbind_when=RegionIn("auth", states={"authenticated"}),
    )
    proxy, identity, _, _ = await _setup(hub_under_test, space, binding=rule)
    await space.auth.verify_session(identity, actor="checker")
    assert await _binding(space, identity) == []
    (operation,) = await _operations(space, identity)
    assert operation.result["proxy_assignment"]["outcome"] == "unbound"
    async with space._tx() as tx:
        row = await tx.get_proxy(proxy.id)
        assert (row.bound_count, row.reserved_count, row.draining_count) == (0, 0, 0)


async def test_state_invalidation_cancels_old_binding_and_live_replacement(hub_under_test, space):
    proxy, identity, _, service = await _setup(hub_under_test, space)
    await space.auth.verify_session(identity, actor="checker")
    (old,) = await _binding(space, identity)
    await space.proxies.create("http://127.0.0.1:8992", group="g", max_identities=1)
    async with space._tx() as tx:
        await tx.update_proxy(proxy.id, {"status": "dead"})
    pool = space.pools.define(
        "replacement",
        service=service,
        holder="worker",
        proxy_policy=ProxyPolicy(group="g", on_proxy_down="rebind"),
    )
    lease = await pool.acquire(among=[identity])
    assert lease.proxy_id != proxy.id
    await space.identities.observe(
        identity, Observation("standing", None, "banned"), actor="checker", source=ChangeSource.PROBE
    )
    assert await _binding(space, identity) == []
    async with space._tx() as tx:
        assert (await tx.proxy_allocation(old.allocation_id)).state == "released"
        assert (await tx.proxy_allocation(lease.proxy_allocation_id)).state == "draining"
        assert (await tx.get_proxy(lease.proxy_id)).draining_count == 1
    with pytest.raises(ProxyAssignmentConflict):
        await lease.confirm_proxy(reason="late accepted result")
    await lease.release()
    async with space._tx() as tx:
        assert (await tx.get_proxy(lease.proxy_id)).draining_count == 0


@pytest.mark.parametrize("pin", [False, True])
async def test_operation_fault_finalizes_accepted_state_without_bypassing_pin(hub_under_test, space, pin):
    proxy, identity, adapter, service = await _setup(hub_under_test, space)
    await space.auth.verify_session(identity, actor="checker")
    if pin:
        await space.identities.override(
            identity,
            Override("standing", None, "good", actor="admin", reason="pin", pin_for=timedelta(hours=1)),
        )
    adapter.fault = Fault(Blame.IDENTITY_BANNED)
    lease = await space.pools.define("worker", service=service, holder="worker").acquire(among=[identity])
    with pytest.raises(RuntimeError, match="domain failure"):
        async with lease.operation("work"):
            raise RuntimeError("domain failure")
    assert len(await _binding(space, identity)) == int(pin)
    async with space._tx() as tx:
        row = await tx.get_proxy(proxy.id)
        assert (row.bound_count, row.draining_count) == (int(pin), int(not pin))
    await lease.release()


async def test_import_update_finalizes_after_all_session_fields(hub_under_test, space):
    rule = ProxyBindingPolicy(
        bind_when=RegionIn("auth", states={"authenticated"}),
        unbind_when=RegionIn("auth", states={"no_session", "session_invalid"}),
    )
    _, identity, _, service = await _setup(hub_under_test, space, binding=rule)
    await space.auth.verify_session(identity, actor="checker")
    report = await space.imports.run(
        service=service,
        records=[ImportRecord(external_ref="a", sessions={"default": {"token": "replacement"}})],
        on_existing="update_session",
        verify_sessions=True,
        actor="importer",
    )
    assert report.updated == [identity] and not report.failed
    assert await _binding(space, identity) == []


async def test_existing_timer_transition_finalizes_unbinding(hub_under_test, space):
    rule = ProxyBindingPolicy(unbind_when=RegionIn("availability", states={"available"}))
    proxy, identity, _, _ = await _setup(hub_under_test, space, binding=rule)
    await space.proxies.rebind(identity, proxy.id, actor="admin", reason="trusted initial binding")
    await space.identities.cool_down(identity, for_=timedelta(milliseconds=1), actor="admin")
    assert len(await _binding(space, identity)) == 1
    if isinstance(hub_under_test.clock, FakeClock):
        hub_under_test.clock.advance(timedelta(milliseconds=2))
    else:
        await asyncio.sleep(0.01)
    await space._expire(identity, "availability")
    assert await _binding(space, identity) == []


async def test_policy_changed_during_probe_keeps_verdict_without_binding(hub_under_test, space, monkeypatch):
    proxy, identity, adapter, service = await _setup(hub_under_test, space)
    adapter.finish = asyncio.Event()
    checking = asyncio.create_task(space.auth.verify_session(identity, actor="checker"))
    async with asyncio.timeout(10):
        await adapter.entered.wait()
        original = hub_under_test.resolve_service
        changed = replace(
            service,
            proxy_policy=ProxyPolicy(
                group="g", binding=ProxyBindingPolicy(bind_when=RegionIn("standing", states={"limited"}))
            ),
        )

        async def resolve(name):
            resolved = await original(name)
            return changed if resolved.name == service.name else resolved

        monkeypatch.setattr(hub_under_test, "resolve_service", resolve)
        adapter.finish.set()
        result = await checking
    assert str(result.outcome) == "valid"
    assert await _binding(space, identity) == []
    (operation,) = await _operations(space, identity)
    assert operation.state == "succeeded"
    assert operation.result["proxy_assignment"]["outcome"] == "policy_changed"
    async with space._tx() as tx:
        assert (await tx.get_proxy(proxy.id)).reserved_count == 0


async def test_accepted_standing_fault_unbinds_even_when_auth_projection_is_pinned(hub_under_test, space):
    _, identity, adapter, _ = await _setup(hub_under_test, space)
    await space.auth.verify_session(identity, actor="checker")
    await space.secrets.replace_credentials(identity, {}, actor="admin")
    await space.identities.override(
        identity,
        Override("auth", None, "authenticated", actor="admin", reason="pin", pin_for=timedelta(hours=1)),
    )
    adapter.result = ProbeResult(AuthState.SESSION_INVALID)
    adapter.login_failure = LoginFailure(
        "account_banned", retryable=False, fault=Fault(Blame.IDENTITY_BANNED)
    )
    result = await space.auth.revalidate(
        identity, actor="checker", reason="check", request_id="selected", credential=("default", "default")
    )
    assert result.applied["auth"] == "pinned"
    current = await space.identities.get(identity)
    assert current.auth == "authenticated" and current.standing == "banned"
    assert await _binding(space, identity) == []
    operations = await _operations(space, identity)
    assert any(
        operation.result.get("proxy_assignment", {}).get("outcome") == "unbound" for operation in operations
    )


async def test_binding_criterion_is_accepted_state_and_not_operation_success(hub_under_test, space):
    rule = ProxyBindingPolicy(
        bind_when=RegionIn("standing", states={"limited"}),
        unbind_when=RegionIn("standing", states={"banned"}),
    )
    proxy, identity, adapter, service = await _setup(hub_under_test, space, binding=rule)
    adapter.fault = Fault(Blame.IDENTITY_BANNED, standing="limited")
    lease = await space.pools.define("worker", service=service, holder="worker").acquire(among=[identity])
    assert lease.proxy_assignment_state == "reserved"
    with pytest.raises(RuntimeError, match="limited"):
        async with lease.operation("work"):
            raise RuntimeError("limited")
    (pair,) = await _binding(space, identity)
    assert pair.proxy_id == proxy.id
    assert (await space.identities.get(identity)).standing == "limited"
    await lease.release()
    assert len(await _binding(space, identity)) == 1


async def test_reconcile_during_probe_keeps_verdict_without_promoting_old_reservation(hub_under_test, space):
    proxy, identity, adapter, service = await _setup(hub_under_test, space)
    adapter.finish = asyncio.Event()
    checking = asyncio.create_task(space.auth.verify_session(identity, actor="checker"))
    async with asyncio.timeout(10):
        await adapter.entered.wait()
        result = await space.proxies.reconcile_bindings(
            [identity],
            service=service.name,
            actor="operator",
            reason="tighten rules",
            proxy_policy=ProxyPolicy(
                group="g", binding=ProxyBindingPolicy(bind_when=RegionIn("standing", states={"limited"}))
            ),
        )
        assert result.applied == [identity]
        adapter.finish.set()
        verdict = await checking
    assert str(verdict.outcome) == "valid"
    assert await _binding(space, identity) == []
    (operation,) = await _operations(space, identity)
    assert (
        operation.state == "succeeded" and operation.result["proxy_assignment"]["outcome"] == "policy_changed"
    )
    async with space._tx() as tx:
        assert (await tx.get_proxy(proxy.id)).reserved_count == 0
        _, sessions = await tx.access_metadata(identity)
        assert sessions[0].last_verified_at is not None
