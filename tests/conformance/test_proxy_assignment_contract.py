"""PA2.1: явный opt-in и резерв вместо ранней постоянной привязки."""

import pytest
from tests.fixtures.api import register

from identity_hub import ProxyBindingPolicy, ProxyPolicy, SpaceHandle, ValidationFailed

pytestmark = pytest.mark.conformance


async def test_new_policy_requires_optin_and_reserves_without_early_binding(space: SpaceHandle) -> None:
    proxy = await space.proxies.create("http://127.0.0.1:9876", group="pending", max_identities=1)
    account = await register(
        space, service="fake_tg", external_ref="pending", lifecycle="ready", session={"auth_key": "k"}
    )
    pending = space.pools.define(
        "pending",
        service="fake_tg",
        proxy_policy=ProxyPolicy(group="pending", binding=ProxyBindingPolicy()),
    )
    with pytest.raises(ValidationFailed, match="allocations accounting"):
        await pending.acquire(among=[account.identity_id], idempotency_key="pending")
    assert (await space.proxies.list(group="pending")).items[0].bound_count == 0

    await space.proxies.configure_group(
        "pending", capacity_accounting="allocations", actor="test", reason="enable"
    )
    async with pending.lease(among=[account.identity_id], idempotency_key="pending") as lease:
        assert lease.proxy_id == proxy.id and lease.proxy_assignment_state == "reserved"
        async with space._tx() as tx:
            assert await tx.proxy_bindings(identity_id=account.identity_id) == []
            assert (await tx.get_proxy(proxy.id)).reserved_count == 1
    async with space._tx() as tx:
        assert await tx.proxy_allocations(identity_id=account.identity_id) == []
        assert (await tx.get_proxy(proxy.id)).reserved_count == 0
    await space.proxies.configure_group(
        "pending", capacity_accounting="bindings", actor="test", reason="restore"
    )
    legacy = space.pools.define("legacy", service="fake_tg", proxy_policy=ProxyPolicy(group="pending"))
    async with legacy.lease(among=[account.identity_id]) as lease:
        assert lease.proxy_id == proxy.id
