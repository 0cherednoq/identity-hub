"""PA3.2: reconcile уважает транзакцию приложения и не оставляет частичный policy update."""

import asyncio

import pytest

from identity_hub import ProxyAssignmentConflict, ProxyBindingPolicy, ProxyPolicy, RegionIn
from identity_hub.read.events import EventFilters
from tests.fixtures.api import register

pytestmark = pytest.mark.pg


async def _setup(space):
    await space.proxies.create("http://127.0.0.1:8997", group="g", max_identities=1)
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
    lease = await pool.acquire(among=[identity])
    await lease.confirm_proxy(reason="accepted")
    return identity, lease


@pytest.mark.parametrize("rollback", [False, True], ids=["commit", "rollback"])
async def test_reconcile_participates_in_application_transaction(space, sessionmaker, rollback):
    identity, lease = await _setup(space)
    async with space._tx() as tx:
        before = await tx.proxy_allocation(lease.proxy_allocation_id)
    policy = ProxyPolicy(
        group="g", binding=ProxyBindingPolicy(unbind_when=RegionIn("standing", states={"limited"}))
    )
    try:
        async with asyncio.timeout(10), sessionmaker() as session, session.begin():
            result = await space.using(session).proxies.reconcile_bindings(
                [identity], service="fake_tg", proxy_policy=policy, actor="operator", reason="new policy"
            )
            assert result.applied == [identity]
            with pytest.raises(ProxyAssignmentConflict, match="policy_changed"):
                await lease.using(session).confirm_proxy(reason="old proof")
            if rollback:
                raise RuntimeError("application rollback")
    except RuntimeError as exc:
        assert rollback and str(exc) == "application rollback"
    async with space._tx() as tx:
        after = await tx.proxy_allocation(before.id)
    events = (
        await space.admin.event_page(filters=EventFilters(type="proxy.binding.policy_reconciled"))
    ).items
    if rollback:
        assert after == before and not events
        assert (await lease.confirm_proxy(reason="rollback restored proof")).state == "bound"
    else:
        assert after.policy_fingerprint != before.policy_fingerprint and len(events) == 1
        with pytest.raises(ProxyAssignmentConflict, match="policy_changed"):
            await lease.confirm_proxy(reason="commit invalidated proof")
    await lease.release()
