"""PA2.2: состояние приложения и назначение фиксируются/откатываются общей сессией."""

import asyncio

import pytest
from sqlalchemy import select

from identity_hub import (
    ChangeSource,
    Observation,
    Override,
    ProxyBindingPolicy,
    ProxyPolicy,
    RegionIn,
    SpaceHandle,
)
from identity_hub.postgres.models import IdentityRecord, ProxyAllocationRecord
from identity_hub.read.events import EventFilters
from tests.fixtures.api import register

pytestmark = pytest.mark.pg


async def _setup(space):
    proxy = await space.proxies.create("http://127.0.0.1:8985", group="g", max_identities=1)
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
        proxy_policy=ProxyPolicy(
            group="g", binding=ProxyBindingPolicy(bind_when=RegionIn("standing", states={"limited"}))
        ),
    )
    return proxy, identity, await pool.acquire(among=[identity])


def _accepted():
    return Override("standing", None, "limited", actor="application", reason="accepted check")


async def test_external_rollback_undoes_state_confirm_and_outbox(space: SpaceHandle, sessionmaker) -> None:
    proxy, identity, lease = await _setup(space)
    with pytest.raises(RuntimeError, match="application rollback"):
        async with asyncio.timeout(10), sessionmaker() as session, session.begin():
            await space.using(session).identities.override(identity, _accepted())
            result = await lease.using(session).confirm_proxy(reason="accepted check")
            assert result.state == "bound"
            assert lease.proxy_assignment_state == "reserved"
            assert (
                await session.execute(select(IdentityRecord.standing).where(IdentityRecord.id == identity))
            ).scalar_one() == "limited"
            raise RuntimeError("application rollback")
    async with space._tx() as tx:
        assert (await tx.get_identity(identity)).standing == "good"
        assert await tx.proxy_bindings(identity_id=identity) == []
        assert (await tx.proxy_allocation(lease.proxy_allocation_id)).state == "reserved"
        row = await tx.get_proxy(proxy.id)
        assert (row.bound_count, row.reserved_count, row.draining_count) == (0, 1, 0)
    assert lease.proxy_assignment_state == "reserved"
    assert not (await space.admin.event_page(filters=EventFilters(type="proxy.binding.changed"))).items
    assert not (await space.admin.event_page(filters=EventFilters(type="proxy.reservation.confirmed"))).items
    await lease.release()


async def test_external_commit_sees_state_and_does_not_prematurely_change_lease(
    space: SpaceHandle, sessionmaker
) -> None:
    proxy, identity, lease = await _setup(space)
    async with asyncio.timeout(10), sessionmaker() as session, session.begin():
        await space.using(session).identities.override(identity, _accepted())
        adapter = lease.using(session)
        assert not hasattr(adapter, "release") and not hasattr(adapter, "renew")
        result = await adapter.confirm_proxy(reason="accepted check")
        assert result.state == "bound" and lease.proxy_assignment_state == "reserved"
    assert lease.proxy_assignment_state == "reserved"  # Adapter не обещает live-обновление snapshot.
    assert await lease.confirm_proxy(reason="refresh after commit") == result
    assert lease.proxy_assignment_state == "bound"
    events = (await space.admin.event_page(filters=EventFilters(type="proxy.binding.changed"))).items
    assert len(events) == 1
    async with space._tx() as tx:
        row = await tx.get_proxy(proxy.id)
        assert (row.bound_count, row.reserved_count) == (1, 0)
    await lease.release()


async def test_outer_savepoint_rollback_can_retry_confirmation_in_same_transaction(
    space: SpaceHandle, sessionmaker
) -> None:
    _, identity, lease = await _setup(space)
    async with asyncio.timeout(10), sessionmaker() as session, session.begin():
        await space.using(session).identities.override(identity, _accepted())
        with pytest.raises(RuntimeError, match="savepoint rollback"):
            async with session.begin_nested():
                await lease.using(session).confirm_proxy(reason="first")
                raise RuntimeError("savepoint rollback")
        state = (
            await session.execute(
                select(ProxyAllocationRecord.state).where(
                    ProxyAllocationRecord.id == lease.proxy_allocation_id
                )
            )
        ).scalar_one()
        assert state == "reserved" and lease.proxy_assignment_state == "reserved"
        assert (await lease.using(session).confirm_proxy(reason="retry")).state == "bound"
    async with space._tx() as tx:
        assert (await tx.proxy_allocation(lease.proxy_allocation_id)).state == "bound"
    assert len((await space.admin.event_page(filters=EventFilters(type="proxy.binding.changed"))).items) == 1
    await lease.release()


@pytest.mark.parametrize("rollback", [False, True], ids=["commit", "rollback"])
async def test_public_observation_and_confirmation_share_application_transaction(
    space, sessionmaker, rollback
):
    _, identity, lease = await _setup(space)
    try:
        async with asyncio.timeout(10), sessionmaker() as session, session.begin():
            decision = await space.using(session).identities.observe(
                identity,
                Observation("standing", "", "limited"),
                actor="checker",
                source=ChangeSource.PROBE,
            )
            assert decision.changed
            assert (await lease.using(session).confirm_proxy(reason="accepted check")).state == "bound"
            assert lease.proxy_assignment_state == "reserved"
            if rollback:
                raise RuntimeError("application rollback")
    except RuntimeError as exc:
        assert rollback and str(exc) == "application rollback"
    async with space._tx() as tx:
        assert (await tx.get_identity(identity)).standing == ("good" if rollback else "limited")
        assert (await tx.proxy_allocation(lease.proxy_allocation_id)).state == (
            "reserved" if rollback else "bound"
        )
    await lease.release()
