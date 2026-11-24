"""PA3.2: новые правила, снимок ledger-политики и старые доказательства."""

from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import BaseModel
from sqlalchemy import func, update
from tests.conftest import TEST_KEYS
from tests.fixtures.api import register
from tests.fixtures.services import FAKE_TG

from identity_hub import (
    IdentityHub,
    Override,
    ProxyAssignmentConflict,
    ProxyBindingPolicy,
    ProxyPolicy,
    RegionIn,
    RevisionConflict,
    RuntimeServiceCreate,
    StaticSecretsProvider,
    ValidationFailed,
)
from identity_hub.domain.idempotency import fingerprint
from identity_hub.postgres.models import OperationKeyRecord
from identity_hub.read.events import EventFilters

pytestmark = pytest.mark.conformance


def _policy(state="banned"):
    return ProxyPolicy(
        group="g",
        binding=ProxyBindingPolicy(
            bind_when=RegionIn("auth", states={"authenticated"}),
            unbind_when=RegionIn("standing", states={state}),
        ),
    )


async def _setup(space, *, bound=True, key=None):
    proxy = await space.proxies.create("http://u:secret@127.0.0.1:8995", group="g", max_identities=1)
    await space.proxies.configure_group("g", capacity_accounting="allocations", actor="test", reason="enable")
    identity = (
        await register(
            space, service="fake_tg", external_ref="a", lifecycle="ready", session={"auth_key": "k"}
        )
    ).identity_id
    pool = space.pools.define("p", service="fake_tg", holder="worker", proxy_policy=_policy())
    lease = await pool.acquire(among=[identity], idempotency_key=key)
    if bound:
        await lease.confirm_proxy(reason="accepted")
    return proxy, identity, pool, lease


async def _reconcile(space, ids, policy=None, **kwargs):
    return await space.proxies.reconcile_bindings(
        ids,
        service="fake_tg",
        proxy_policy=policy or _policy("limited"),
        actor="operator",
        reason="new rules",
        **kwargs,
    )


async def _row(space, allocation):
    async with space._tx() as tx:
        return await tx.proxy_allocation(allocation)


async def _events(space):
    return (await space.admin.event_page(filters=EventFilters(type="proxy.binding.policy_reconciled"))).items


async def test_reconcile_unbinds_current_state_and_is_idempotent(space):
    proxy, identity, _, lease = await _setup(space)
    await lease.release()
    await space.identities.override(
        identity, Override("standing", None, "limited", actor="operator", reason="known")
    )
    result = await _reconcile(space, [identity])
    assert result.applied == [identity] and not result.rejected
    assert (await _row(space, lease.proxy_allocation_id)).state == "released"
    async with space._tx() as tx:
        assert await tx.proxy_bindings(identity_id=identity) == []
        row = await tx.get_proxy(proxy.id)
        assert (row.bound_count, row.reserved_count, row.draining_count) == (0, 0, 0)
    (event,) = await _events(space)
    assert event.actor == "operator" and event.data["reason"] == "new rules"
    assert event.data["binding_id"] == str(lease.proxy_allocation_id) and event.data["outcome"] == "unbound"
    await _reconcile(space, [identity])
    assert len(await _events(space)) == 1


async def test_policy_refresh_keeps_generation_and_one_slot(space):
    proxy, identity, _, lease = await _setup(space)
    await lease.release()
    before = await _row(space, lease.proxy_allocation_id)
    await _reconcile(space, [identity])
    after = await _row(space, before.id)
    assert before.id == after.id and before.proxy_id == after.proxy_id and after.state == "bound"
    assert after.policy_fingerprint != before.policy_fingerprint and "reconciliation_id" in after.policy
    async with space._tx() as tx:
        assert (await tx.proxy_bindings(identity_id=identity))[0].allocation_id == before.id
        row = await tx.get_proxy(proxy.id)
        assert (row.bound_count, row.reserved_count, row.draining_count) == (1, 0, 0)
    await _reconcile(space, [identity])
    assert await _row(space, before.id) == after and len(await _events(space)) == 1


@pytest.mark.parametrize("bound", [False, True])
async def test_old_use_and_replay_never_receive_new_policy_capability(space, bound):
    _, identity, pool, lease = await _setup(space, bound=bound, key="job")
    request = lease._row.proxy_request
    await _reconcile(space, [identity])
    with pytest.raises(ProxyAssignmentConflict, match="policy_changed"):
        await lease.confirm_proxy(reason="late result")
    replay = await pool.acquire(idempotency_key="job")
    assert (replay.lease_id, replay.proxy_id, replay._row.proxy_request) == (
        lease.lease_id,
        lease.proxy_id,
        request,
    )
    with pytest.raises(ProxyAssignmentConflict, match="policy_changed"):
        await replay.confirm_proxy(reason="late replay")
    await replay.release()
    await lease.release()
    if bound:
        fresh = await space.pools.define(
            "new", service="fake_tg", holder="worker", proxy_policy=_policy("limited")
        ).acquire(among=[identity])
        assert (await fresh.confirm_proxy(reason="new use")).state == "bound"
        await fresh.release()


async def test_restoring_same_policy_does_not_restore_old_capability(space):
    _, identity, _, lease = await _setup(space)
    original = await _row(space, lease.proxy_allocation_id)
    await _reconcile(space, [identity], _policy("limited"))
    await _reconcile(space, [identity], _policy())
    assert (await _row(space, original.id)).policy_fingerprint != original.policy_fingerprint
    with pytest.raises(ProxyAssignmentConflict, match="policy_changed"):
        await lease.confirm_proxy(reason="late original policy")
    await lease.release()


@pytest.mark.parametrize("bound", [False, True])
async def test_negative_reconcile_drains_live_reservation(space, bound):
    proxy, identity, _, lease = await _setup(space, bound=bound)
    await _reconcile(space, [identity], _policy("good"))
    assert (await _row(space, lease.proxy_allocation_id)).state == "draining"
    async with space._tx() as tx:
        row = await tx.get_proxy(proxy.id)
        assert (row.bound_count, row.reserved_count, row.draining_count) == (0, 0, 1)
        assert await tx.proxy_bindings(identity_id=identity) == []
    await lease.release()
    assert (await _row(space, lease.proxy_allocation_id)).state == "released"


async def test_reconcile_never_creates_or_confirms_binding(space):
    _, identity, _, lease = await _setup(space, bound=False)
    await _reconcile(space, [identity])
    assert (await _row(space, lease.proxy_allocation_id)).state == "reserved"
    await lease.release()
    await _reconcile(space, [identity])
    async with space._tx() as tx:
        assert await tx.proxy_bindings(identity_id=identity) == []
        assert await tx.proxy_allocations(identity_id=identity) == []


async def test_stale_generation_and_foreign_identity_are_rejected(space):
    _, identity, _, lease = await _setup(space)
    before = await _row(space, lease.proxy_allocation_id)
    result = await _reconcile(space, [identity], expected_bindings={identity: uuid4()})
    assert result.rejected[0].code == "proxy_assignment_conflict" and not result.applied
    assert await _row(space, before.id) == before
    foreign = space.hub.space("foreign-policy-reconcile", create=True)
    result = await _reconcile(foreign, [identity])
    assert result.rejected[0].code == "not_found"
    assert await _row(space, before.id) == before
    await lease.release()


@pytest.mark.parametrize("version", [3, True, "2"])
async def test_unknown_ledger_policy_is_not_reinterpreted(space, version):
    _, identity, _, lease = await _setup(space)
    async with space._tx() as tx:
        row = await tx.proxy_allocation(lease.proxy_allocation_id)
        await tx.proxy_store_allocation(replace(row, policy={**row.policy, "version": version}))
    result = await _reconcile(space, [identity])
    assert result.rejected[0].code == "proxy_assignment_conflict"
    with pytest.raises(ProxyAssignmentConflict, match="policy_changed"):
        await lease.confirm_proxy(reason="unknown version")
    await lease.release()


async def test_missing_subject_field_is_not_administrative_null_capability(space):
    proxy, identity, _, lease = await _setup(space)
    await lease.release()
    await space.proxies.unbind(identity, actor="admin", reason="replace")
    await space.proxies.rebind(identity, proxy.id, actor="admin", reason="trusted")
    active = await space.pools.define(
        "admin-bound", service="fake_tg", holder="worker", proxy_policy=_policy()
    ).acquire(among=[identity])
    assert active._row.proxy_request["allocation_policy_fingerprint"] is None
    async with space._tx() as tx:
        row = await tx.proxy_allocation(active.proxy_allocation_id)
        request = dict(active._row.proxy_request)
        request.pop("allocation_policy_fingerprint")
        await tx.proxy_attach_lease(active.fence, row, request=request)
    with pytest.raises(ProxyAssignmentConflict, match="policy_changed"):
        await active.confirm_proxy(reason="missing proof")
    await active.release()


@pytest.mark.parametrize("revision", [True, 0, -1, "1"])
async def test_revision_shape_validated_before_mutation(space, revision):
    _, identity, _, lease = await _setup(space)
    with pytest.raises(ValidationFailed):
        await _reconcile(space, [identity], expected_service_revision=revision)
    assert not await _events(space)
    await lease.release()


async def test_static_service_does_not_accept_arbitrary_runtime_revision(space):
    _, identity, _, lease = await _setup(space)
    with pytest.raises(RevisionConflict):
        await _reconcile(space, [identity], expected_service_revision=1)
    await lease.release()


async def test_expected_bindings_cannot_silently_leave_rows_unfenced(space):
    _, identity, _, lease = await _setup(space)
    with pytest.raises(ValidationFailed, match="exactly"):
        await _reconcile(space, [identity], expected_bindings={})
    assert not await _events(space)
    await lease.release()


async def test_known_v1_bound_can_issue_v2_use_and_be_explicitly_upgraded(space):
    _, identity, pool, lease = await _setup(space)
    await lease.release()
    async with space._tx() as tx:
        row = await tx.proxy_allocation(lease.proxy_allocation_id)
        old_policy = {**row.policy, "version": 1}
        await tx.proxy_store_allocation(
            replace(row, policy=old_policy, policy_fingerprint=fingerprint("proxy_assignment", old_policy))
        )
    active = await pool.acquire(among=[identity])
    assert active._row.proxy_request["version"] == 2
    assert (await active.confirm_proxy(reason="v2 use on known v1 binding")).state == "bound"
    await _reconcile(space, [identity], _policy())
    assert (await _row(space, active.proxy_allocation_id)).policy["version"] == 2
    with pytest.raises(ProxyAssignmentConflict, match="policy_changed"):
        await active.confirm_proxy(reason="reconcile invalidated old proof")
    await active.release()


@pytest.mark.parametrize("version", [1, 2.0, True, "2"])
async def test_old_or_non_integer_use_version_cannot_confirm(space, version):
    _, _, _, lease = await _setup(space)
    async with space._tx() as tx:
        row = await tx.proxy_allocation(lease.proxy_allocation_id)
        request = {**lease._row.proxy_request, "version": version}
        if version == 1:
            request.pop("allocation_policy_fingerprint")
        await tx.proxy_attach_lease(lease.fence, row, request=request)
    with pytest.raises(ProxyAssignmentConflict, match="policy_changed"):
        await lease.confirm_proxy(reason="unproven request version")
    await lease.release()


async def test_expired_acquire_key_does_not_refresh_old_capability(
    space, hub_under_test, backend_sessionmaker
):
    _, identity, pool, lease = await _setup(space, key="job")
    await _reconcile(space, [identity])
    if backend_sessionmaker is None:
        store = hub_under_test._store
        for slot, row in list(store._state.operation_keys.items()):
            store._state.operation_keys[slot] = replace(
                row, expires_at=hub_under_test.clock.now() - timedelta(seconds=1)
            )
    else:
        async with backend_sessionmaker.begin() as session:
            await session.execute(
                update(OperationKeyRecord)
                .where(OperationKeyRecord.key == "job")
                .values(expires_at=func.now() - timedelta(seconds=1))
            )
    replay = await pool.acquire(idempotency_key="job")
    assert replay._row.proxy_request == lease._row.proxy_request
    with pytest.raises(ProxyAssignmentConflict, match="policy_changed"):
        await replay.confirm_proxy(reason="replay after key expiry")
    await replay.release()
    await lease.release()


@pytest.mark.parametrize("atomic", [False, True])
async def test_wrong_service_partial_and_atomic_batches(space, atomic):
    _, identity, _, lease = await _setup(space)
    await lease.release()
    other = (
        await register(
            space, service="fake_exchange", external_ref="b", lifecycle="ready", session={"cookies": {}}
        )
    ).identity_id
    before = await _row(space, lease.proxy_allocation_id)
    if atomic:
        with pytest.raises(ValidationFailed):
            await _reconcile(space, [identity, other], atomic=True)
        assert await _row(space, before.id) == before
        assert not await _events(space)
    else:
        result = await _reconcile(space, [identity, other])
        assert result.applied == [identity]
        assert result.rejected[0].key == other and result.rejected[0].code == "validation_error"
        assert (await _row(space, before.id)).policy_fingerprint != before.policy_fingerprint


async def test_explicit_removal_of_pool_rules_preserves_physical_binding(space):
    _, identity, _, lease = await _setup(space)
    await lease.release()
    result = await space.proxies.reconcile_bindings(
        [identity], service="fake_tg", actor="operator", reason="remove pool rules"
    )
    assert result.applied == [identity]
    row = await _row(space, lease.proxy_allocation_id)
    assert row.state == "bound" and row.policy["policy"] is None
    await space.identities.override(
        identity, Override("standing", None, "banned", actor="operator", reason="new state")
    )
    async with space._tx() as tx:
        assert len(await tx.proxy_bindings(identity_id=identity)) == 1


async def test_draining_policy_does_not_control_reconciled_new_generation(space):
    _, identity, _, old_use = await _setup(space)
    other = await space.proxies.create("http://127.0.0.1:8998", group="g", max_identities=1)
    await space.proxies.rebind(identity, other.id, actor="admin", reason="new generation")
    await _reconcile(space, [identity], _policy("limited"))
    await space.identities.override(
        identity, Override("standing", None, "banned", actor="operator", reason="old rule only")
    )
    async with space._tx() as tx:
        assert (await tx.get_binding(identity)).id == other.id
        assert (await tx.proxy_allocation(old_use.proxy_allocation_id)).state == "draining"
        assert (await tx.get_proxy(other.id)).bound_count == 1
    await old_use.release()


class Config(BaseModel):
    unbind: str = "banned"


class Template:
    name = "proxy-reconcile/v1"
    config_schema = Config

    def build(self, definition):
        return replace(
            FAKE_TG,
            name=definition.name,
            title=definition.title,
            url=definition.url,
            proxy_policy=_policy(definition.config["unbind"]),
        )


async def test_current_runtime_revision_and_service_constraints_are_mandatory(space, hub_under_test):
    runtime = IdentityHub(
        store=hub_under_test._store,
        secrets=StaticSecretsProvider(TEST_KEYS, "k1"),
        service_templates=[Template()],
        secret_storage="encrypted",
    )
    await runtime.start()
    space = runtime.space("reconcile-runtime", create=True)
    definition, _ = await runtime.service_definitions.ensure(
        RuntimeServiceCreate(name="dynamic", title="Dynamic", url=None, template=Template.name, config={}),
        actor="test",
        idempotency_key="definition",
    )
    await space.proxies.create("http://127.0.0.1:8996", group="g", max_identities=1)
    await space.proxies.configure_group("g", capacity_accounting="allocations", actor="test", reason="enable")
    identity = (
        await register(
            space, service="dynamic", external_ref="a", lifecycle="ready", session={"auth_key": "k"}
        )
    ).identity_id
    lease = await space.pools.define("p", service="dynamic", holder="worker").acquire(among=[identity])
    await lease.confirm_proxy(reason="accepted")
    await lease.release()
    old = await _row(space, lease.proxy_allocation_id)
    current = await runtime.service_definitions.update(
        "dynamic", config={"unbind": "limited"}, expected_revision=definition.revision, actor="operator"
    )
    with pytest.raises(RevisionConflict):
        await space.proxies.reconcile_bindings(
            [identity],
            service="dynamic",
            expected_service_revision=definition.revision,
            actor="operator",
            reason="stale revision",
        )
    await space.identities.override(
        identity, Override("standing", None, "limited", actor="operator", reason="known")
    )
    assert (await _row(space, old.id)).state == "bound"  # Новая конфигурация не запускает массовый reconcile.
    result = await space.proxies.reconcile_bindings(
        [identity],
        service="dynamic",
        expected_service_revision=current.revision,
        proxy_policy=ProxyPolicy(group="g"),
        actor="operator",
        reason="current revision",
    )
    assert result.applied == [identity]
    assert (
        await _row(space, old.id)
    ).state == "released"  # Override не отключил отрицательное правило Service.
