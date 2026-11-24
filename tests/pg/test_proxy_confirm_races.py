"""PA2.2/PA2.3: точка сериализации confirm с отзывом и сменой поколения."""

import asyncio

import pytest
from sqlalchemy import func, select

from identity_hub import (
    LeaseOwnershipLost,
    ProxyAssignmentConflict,
    ProxyBindingPolicy,
    ProxyPolicy,
    RegionIn,
)
from identity_hub.postgres.store import PgTx
from tests.fixtures.api import register

pytestmark = pytest.mark.pg


@pytest.mark.parametrize("first", ["confirm", "admin"])
@pytest.mark.parametrize("operation", ["revoke", "replace", "reconcile"])
async def test_confirm_serializes_with_admin_at_identity_lock(
    space, sessionmaker, monkeypatch, first, operation
):
    proxies = [
        await space.proxies.create(f"http://127.0.0.1:{8987 + n}", group="g", max_identities=1)
        for n in range(2)
    ]
    await space.proxies.configure_group("g", capacity_accounting="allocations", actor="test", reason="enable")
    identity = (
        await register(
            space, service="fake_tg", external_ref="a", lifecycle="ready", session={"auth_key": "k"}
        )
    ).identity_id
    lease = await space.pools.define(
        "p",
        service="fake_tg",
        holder="worker",
        proxy_policy=ProxyPolicy(group="g", binding=ProxyBindingPolicy()),
    ).acquire(among=[identity])
    replacement = next(proxy for proxy in proxies if proxy.id != lease.proxy_id)
    ready, entered, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    pids = {}
    seen = set()
    original_check, original_lock = PgTx.check_fence, PgTx.lock_identities

    async def gate(tx, *, before):
        name = asyncio.current_task().get_name()
        if (name, before) in seen:
            return
        seen.add((name, before))
        if before and name == "contender":
            pids[name] = (await tx.execute(select(func.pg_backend_pid()))).scalar_one()
            entered.set()
        if not before and name == "owner":
            pids[name] = (await tx.execute(select(func.pg_backend_pid()))).scalar_one()
            ready.set()
            await finish.wait()

    async def check(tx, fence, *, lock=True):
        await gate(tx, before=True)
        await original_check(tx, fence, lock=lock)
        await gate(tx, before=False)

    async def lock(tx, ids):
        await gate(tx, before=True)
        await original_lock(tx, ids)
        await gate(tx, before=False)

    monkeypatch.setattr(PgTx, "check_fence", check)
    monkeypatch.setattr(PgTx, "lock_identities", lock)

    async def confirm():
        return await lease.confirm_proxy(reason="accepted")

    async def admin():
        if operation == "revoke":
            return await space.pools.revoke(identity, actor="admin", reason="stop")
        if operation == "reconcile":
            return await space.proxies.reconcile_bindings(
                [identity],
                service="fake_tg",
                actor="admin",
                reason="new rules",
                proxy_policy=ProxyPolicy(
                    group="g", binding=ProxyBindingPolicy(bind_when=RegionIn("standing", states={"good"}))
                ),
            )
        return await space.proxies.rebind(identity, replacement.id, actor="admin", reason="replace")

    actions = {"confirm": confirm, "admin": admin}
    second = "admin" if first == "confirm" else "confirm"
    async with asyncio.timeout(10):
        owner = asyncio.create_task(actions[first](), name="owner")
        await ready.wait()
        contender = asyncio.create_task(actions[second](), name="contender")
        try:
            await entered.wait()
            async with sessionmaker() as observer:
                while (
                    pids["owner"]
                    not in (
                        await observer.execute(select(func.pg_blocking_pids(pids["contender"])))
                    ).scalar_one()
                ):
                    pass
            assert not contender.done()
        finally:
            finish.set()
        results = await asyncio.gather(owner, contender, return_exceptions=True)
    assert not isinstance(results[0], BaseException)
    if first == "admin":
        error = LeaseOwnershipLost if operation == "revoke" else ProxyAssignmentConflict
        assert isinstance(results[1], error)
        if operation == "replace":
            assert results[1].reason == "assignment_changed"
        elif operation == "reconcile":
            assert results[1].reason == "policy_changed"
    else:
        assert not isinstance(results[1], BaseException)
    async with space._tx() as tx:
        old = await tx.proxy_allocation(lease.proxy_allocation_id)
        if operation == "revoke":
            assert old.state == ("bound" if first == "confirm" else "released")
        elif operation == "reconcile":
            assert old.state == ("bound" if first == "confirm" else "reserved")
            assert "reconciliation_id" in old.policy
        else:
            assert old.state == "draining"
            assert (await tx.get_binding(identity)).id == replacement.id
    if operation == "replace":
        await lease.release()
        async with space._tx() as tx:
            assert (await tx.proxy_allocation(lease.proxy_allocation_id)).state == "released"
            assert (await tx.get_proxy(replacement.id)).bound_count == 1
    elif operation == "reconcile":
        await lease.release()
