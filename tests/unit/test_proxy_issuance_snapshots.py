"""Снимок выдачи не меняется при обновлении runtime Service и повторном использовании Pool."""

from dataclasses import replace
from datetime import timedelta

import pytest
from pydantic import BaseModel

from identity_hub import (
    IdempotencyConflict,
    IdentityHub,
    ProxyAssignmentConflict,
    ProxyBindingPolicy,
    ProxyPolicy,
    RegionIn,
    RuntimeServiceCreate,
    StaticSecretsProvider,
)
from identity_hub.domain.idempotency import LEASE_FAMILY, fingerprint, namespace
from identity_hub.store.base import IdempotencyRequest
from identity_hub.testing import MemoryStore
from tests.fixtures.api import register
from tests.fixtures.services import FAKE_TG

pytestmark = pytest.mark.unit


class Config(BaseModel):
    standing: str = "good"


class Template:
    name = "proxy-snapshots/v1"
    config_schema = Config

    def build(self, definition):
        return replace(
            FAKE_TG,
            name=definition.name,
            title=definition.title,
            url=definition.url,
            proxy_policy=ProxyPolicy(
                group="g",
                binding=ProxyBindingPolicy(
                    bind_when=RegionIn("standing", states={definition.config["standing"]}),
                ),
            ),
        )


async def test_runtime_refresh_recomposes_original_override_and_keeps_older_lease_snapshot() -> None:
    hub = IdentityHub(
        store=MemoryStore(),
        secrets=StaticSecretsProvider({"k": bytes(range(32))}, "k"),
        service_templates=[Template()],
    )
    await hub.start()
    definition, _ = await hub.service_definitions.ensure(
        RuntimeServiceCreate(name="dynamic", title="Dynamic", url=None, template=Template.name, config={}),
        actor="test",
        idempotency_key="definition",
    )
    space = await hub.spaces.ensure("snapshots")
    await space.proxies.create("http://127.0.0.1:8970", group="g", max_identities=2)
    await space.proxies.configure_group("g", capacity_accounting="allocations", actor="test", reason="enable")
    identities = [
        (
            await register(
                space, service="dynamic", external_ref=ref, lifecycle="ready", session={"auth_key": "k"}
            )
        ).identity_id
        for ref in ("a", "b")
    ]
    pool = space.pools.define(
        "p",
        service="dynamic",
        holder="worker",
        proxy_policy=ProxyPolicy(
            group="g",
            binding=ProxyBindingPolicy(bind_when=RegionIn("auth", states={"authenticated"})),
        ),
    )
    first = await pool.acquire(among=[identities[0]], idempotency_key="first")
    before = first.pool.proxy_policy.binding.to_dict()
    await hub.service_definitions.update(
        "dynamic", config={"standing": "limited"}, expected_revision=definition.revision, actor="test"
    )
    second = await pool.acquire(among=[identities[1]], idempotency_key="second")
    after = second.pool.proxy_policy.binding.to_dict()
    assert first.pool.proxy_policy.binding.to_dict() == before
    assert before["bind_when"]["rules"][0]["states"] == ["good"]
    assert after["bind_when"]["rules"][0]["states"] == ["limited"]
    assert len(after["bind_when"]["rules"]) == 2
    replay = await pool.acquire(idempotency_key="second")
    assert replay.lease_id == second.lease_id
    assert len(replay.pool.proxy_policy.binding.to_dict()["bind_when"]["rules"]) == 2
    assert first._row.proxy_request["service_revision"] == 1
    assert second._row.proxy_request["service_revision"] == 2
    await hub.service_definitions.update("dynamic", title="Renamed", expected_revision=2, actor="test")
    with pytest.raises(IdempotencyConflict):
        await pool.acquire(idempotency_key="second")
    assert second.pool._service_revision == 2
    with pytest.raises(ProxyAssignmentConflict, match="policy_changed"):
        await second.confirm_proxy(reason="result from previous revision")
    assert second.proxy_assignment_state == "reserved"
    with pytest.raises(IdempotencyConflict):
        await pool.acquire(idempotency_key="first")
    await first.release()
    await second.release()


@pytest.mark.parametrize("outcome", ["failed", "succeeded"])
async def test_memory_legacy_key_never_authorizes_strict_assignment(outcome: str) -> None:
    store = MemoryStore()
    hub = IdentityHub(
        store=store, secrets=StaticSecretsProvider({"k": bytes(range(32))}, "k"), services=[FAKE_TG]
    )
    await hub.start()
    space = await hub.spaces.ensure("old-keys")
    await space.proxies.create("http://127.0.0.1:8975", group="g", max_identities=1)
    await space.proxies.configure_group("g", capacity_accounting="allocations", actor="test", reason="enable")
    identity = (
        await register(
            space, service="fake_tg", external_ref="a", lifecycle="ready", session={"auth_key": "k"}
        )
    ).identity_id
    pool = space.pools.define(
        "p",
        service="fake_tg",
        holder="worker",
        proxy_policy=ProxyPolicy(group="g", binding=ProxyBindingPolicy()),
    )
    key_namespace = namespace((await space.ref()).id, LEASE_FAMILY)
    async with space._tx() as tx:
        await tx.claim_idempotency_key(
            IdempotencyRequest(
                key_namespace, "old", LEASE_FAMILY, fingerprint(LEASE_FAMILY, {}), timedelta(days=1)
            )
        )
        await tx.complete_idempotency_key(key_namespace, "old", outcome)
    key = (key_namespace, "old")
    store._state.operation_keys[key] = replace(store._state.operation_keys[key], fingerprint_version=0)
    with pytest.raises(IdempotencyConflict):
        await pool.acquire(among=[identity], idempotency_key="old")
    async with space._tx() as tx:
        assert await tx.proxy_allocations(identity_id=identity) == []
