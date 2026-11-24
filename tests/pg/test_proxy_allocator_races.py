"""PA1.2: пересечения транзакций подтверждены барьерами и pg_locks."""

import asyncio

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub import (
    ConcurrentModification,
    ImportRecord,
    LeaseUnavailable,
    ProxyBindingPolicy,
    ProxyPolicy,
    SpaceHandle,
)
from identity_hub.postgres.models import ProxyRecord
from identity_hub.postgres.store import PgStore, PgTx
from identity_hub.proxy_allocator import reserve
from identity_hub.read.events import EventFilters
from tests.fixtures.api import register

pytestmark = pytest.mark.pg


async def test_last_slot_competitors_both_select_before_lock(
    space: SpaceHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = await space.proxies.create("http://127.0.0.1:8920", group="g", max_identities=1)
    await space.proxies.configure_group("g", capacity_accounting="allocations", actor="test", reason="switch")
    identities = [
        (
            await register(
                space, service="fake_tg", external_ref=ref, lifecycle="ready", session={"auth_key": "k"}
            )
        ).identity_id
        for ref in ("a", "b")
    ]
    pool = space.pools.define("p", service="fake_tg")
    leases = [await pool.acquire(among=[identity]) for identity in identities]
    original = PgTx.proxy_lock_rows
    ready = asyncio.Event()
    arrivals = 0

    async def barrier(self, ids):
        nonlocal arrivals
        assert proxy.id in ids
        arrivals += 1
        if arrivals == 2:
            ready.set()
        await ready.wait()
        await original(self, ids)

    monkeypatch.setattr(PgTx, "proxy_lock_rows", barrier)

    async def contender(index):
        async with space._tx() as tx:
            return await reserve(tx, identities[index], "g", owner_lease_id=leases[index].lease_id)

    async with asyncio.timeout(10):
        outcomes = await asyncio.gather(contender(0), contender(1), return_exceptions=True)
    assert arrivals == 2
    assert sum(isinstance(outcome, LeaseUnavailable) for outcome in outcomes) == 1
    assert sum(not isinstance(outcome, BaseException) for outcome in outcomes) == 1
    for lease in leases:
        await lease.release()


async def test_configuration_waits_for_existing_space_transaction(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    await space.proxies.create("http://127.0.0.1:8921", group="g")
    original = PgStore._bind
    config_entered = asyncio.Event()
    transaction_ready = asyncio.Event()
    finish = asyncio.Event()
    pids = {}

    async def bind(session, ref, *, proxy_configuration=None):
        if proxy_configuration:
            pids["config"] = (await session.execute(select(func.pg_backend_pid()))).scalar_one()
            config_entered.set()
        await original(session, ref, proxy_configuration=proxy_configuration)

    monkeypatch.setattr(PgStore, "_bind", staticmethod(bind))

    async def transaction():
        async with space._tx() as tx:
            pids["owner"] = (await tx.session.execute(select(func.pg_backend_pid()))).scalar_one()
            transaction_ready.set()
            await finish.wait()

    holding = asyncio.create_task(transaction())
    await transaction_ready.wait()
    config = asyncio.create_task(
        space.proxies.configure_group("g", capacity_accounting="allocations", actor="test", reason="switch")
    )
    try:
        await config_entered.wait()
        async with asyncio.timeout(10), sessionmaker() as observer:
            while True:
                blockers = (
                    await observer.execute(select(func.pg_blocking_pids(pids["config"])))
                ).scalar_one()
                if pids["owner"] in blockers:
                    break
        assert not config.done()
    finally:
        finish.set()
        await holding
    assert (await config).capacity_accounting == "allocations"


async def test_reserve_rechecks_group_after_select(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = await space.proxies.create("http://127.0.0.1:8922", group="g", max_identities=1)
    await space.proxies.create("http://127.0.0.1:8923", group="legacy")
    await space.proxies.configure_group("g", capacity_accounting="allocations", actor="test", reason="switch")
    identity = (
        await register(
            space, service="fake_tg", external_ref="a", lifecycle="ready", session={"auth_key": "k"}
        )
    ).identity_id
    lease = await space.pools.define("p", service="fake_tg").acquire(among=[identity])
    original = PgTx.proxy_lock_rows
    moved = False

    async def move_between_read_and_lock(self, ids):
        nonlocal moved
        if not moved:
            async with sessionmaker.begin() as session:
                await session.execute(
                    update(ProxyRecord).where(ProxyRecord.id == proxy.id).values(pool="legacy")
                )
            moved = True
        await original(self, ids)

    monkeypatch.setattr(PgTx, "proxy_lock_rows", move_between_read_and_lock)
    with pytest.raises(LeaseUnavailable):
        async with space._tx() as tx:
            await reserve(tx, identity, "g", owner_lease_id=lease.lease_id)
    async with space._tx() as tx:
        assert (await tx.get_proxy(proxy.id)).pool == "legacy"
        assert (await tx.get_proxy(proxy.id)).reserved_count == 0
        assert await tx.proxy_allocations(identity_id=identity) == []
    await lease.release()


async def test_reserve_and_admin_rebind_compete_for_one_slot(
    space: SpaceHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = await space.proxies.create("http://127.0.0.1:8924", group="g", max_identities=1)
    await space.proxies.configure_group("g", capacity_accounting="allocations", actor="test", reason="switch")
    identities = [
        (
            await register(
                space, service="fake_tg", external_ref=ref, lifecycle="ready", session={"auth_key": "k"}
            )
        ).identity_id
        for ref in ("a", "b")
    ]
    lease = await space.pools.define("p", service="fake_tg").acquire(among=[identities[0]])
    original = PgTx.proxy_lock_rows
    ready = asyncio.Event()
    arrived = set()

    async def barrier(self, ids):
        if proxy.id in ids and id(self) not in arrived:
            arrived.add(id(self))
            if len(arrived) == 2:
                ready.set()
            await ready.wait()
        await original(self, ids)

    monkeypatch.setattr(PgTx, "proxy_lock_rows", barrier)

    async def reservation():
        async with space._tx() as tx:
            return await reserve(tx, identities[0], "g", owner_lease_id=lease.lease_id)

    async with asyncio.timeout(10):
        results = await asyncio.gather(
            reservation(),
            space.proxies.rebind(identities[1], proxy.id, actor="admin", reason="trusted"),
            return_exceptions=True,
        )
    assert len(arrived) == 2
    assert sum(isinstance(result, LeaseUnavailable) for result in results) == 1
    assert sum(not isinstance(result, BaseException) for result in results) == 1
    async with space._tx() as tx:
        row = await tx.get_proxy(proxy.id)
        assert row.bound_count + row.reserved_count + row.draining_count == 1
    await lease.release()


async def test_disable_rechecks_identity_set_instead_of_locking_it_after_proxy(
    space: SpaceHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = await space.proxies.create("http://127.0.0.1:8925", group="g", max_identities=1)
    await space.proxies.configure_group("g", capacity_accounting="allocations", actor="test", reason="switch")
    identity = (await register(space, service="fake_tg", external_ref="a")).identity_id
    original = PgTx.proxy_lock_rows
    snapshot_taken = asyncio.Event()
    proceed = asyncio.Event()

    async def barrier(self, ids):
        if asyncio.current_task().get_name() == "disable-before-proxy-lock":
            snapshot_taken.set()
            await proceed.wait()
        await original(self, ids)

    monkeypatch.setattr(PgTx, "proxy_lock_rows", barrier)
    disabling = asyncio.create_task(
        space.proxies.disable(proxy.id, actor="admin", reason="maintenance", unbind=True),
        name="disable-before-proxy-lock",
    )
    try:
        async with asyncio.timeout(10):
            await snapshot_taken.wait()
            assert await space.proxies.rebind(identity, proxy.id, actor="admin", reason="new binding")
            proceed.set()
            with pytest.raises(ConcurrentModification):
                await disabling
    finally:
        proceed.set()
        if not disabling.done():
            disabling.cancel()
            await asyncio.gather(disabling, return_exceptions=True)
    async with space._tx() as tx:
        assert (await tx.get_proxy(proxy.id)).enabled
        assert (await tx.proxy_bindings(identity_id=identity))[0].proxy_id == proxy.id


async def test_rebind_batches_with_opposite_target_order_finish_without_deadlock(
    space: SpaceHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxies = [
        await space.proxies.create(f"http://127.0.0.1:{port}", group="g", max_identities=2)
        for port in (8927, 8928)
    ]
    await space.proxies.configure_group("g", capacity_accounting="allocations", actor="test", reason="switch")
    identities = [
        (await register(space, service="fake_tg", external_ref=str(index))).identity_id for index in range(4)
    ]
    left, right = sorted(identities[:2]), sorted(identities[2:])
    targets = [dict(zip(left, (row.id for row in proxies), strict=True))]
    targets.append(dict(zip(right, (row.id for row in reversed(proxies)), strict=True)))
    original = PgTx.proxy_lock_rows
    ready = asyncio.Event()
    arrived = set()

    async def barrier(self, ids):
        if ids and id(self) not in arrived:
            arrived.add(id(self))
            if len(arrived) == 2:
                ready.set()
            await ready.wait()
        await original(self, ids)

    monkeypatch.setattr(PgTx, "proxy_lock_rows", barrier)
    async with asyncio.timeout(10):
        results = await asyncio.gather(
            *(space.proxies.rebind_many(batch, actor="admin", reason="trusted") for batch in targets)
        )
    assert all(len(result.applied) == 2 and result.rejected == [] for result in results)
    async with space._tx() as tx:
        for proxy in proxies:
            assert (await tx.get_proxy(proxy.id)).bound_count == 2


async def test_import_and_reserve_compete_for_same_physical_slot(
    space: SpaceHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = await space.proxies.create("http://127.0.0.1:8931", group="g", max_identities=1)
    await space.proxies.configure_group("g", capacity_accounting="allocations", actor="test", reason="switch")
    identity = (
        await register(
            space, service="fake_tg", external_ref="owner", lifecycle="ready", session={"auth_key": "k"}
        )
    ).identity_id
    lease = await space.pools.define("p", service="fake_tg").acquire(among=[identity])
    original = PgTx.proxy_lock_rows
    ready = asyncio.Event()
    arrived = set()

    async def barrier(self, ids):
        if proxy.id in ids and id(self) not in arrived:
            arrived.add(id(self))
            if len(arrived) == 2:
                ready.set()
            await ready.wait()
        await original(self, ids)

    monkeypatch.setattr(PgTx, "proxy_lock_rows", barrier)

    async def reservation():
        async with space._tx() as tx:
            return await reserve(tx, identity, "g", owner_lease_id=lease.lease_id)

    async with asyncio.timeout(10):
        reserved, imported = await asyncio.gather(
            reservation(),
            space.imports.run(
                service="fake_tg",
                records=[ImportRecord("imported", proxy="http://127.0.0.1:8931")],
                actor="admin",
            ),
            return_exceptions=True,
        )
    assert not isinstance(imported, BaseException)
    assert len(arrived) == 2
    if isinstance(reserved, LeaseUnavailable):
        assert len(imported.created) == 1 and not imported.failed
    else:
        assert not isinstance(reserved, BaseException)
        assert not imported.created and len(imported.failed) == 1
    async with space._tx() as tx:
        row = await tx.get_proxy(proxy.id)
        assert row.bound_count + row.reserved_count + row.draining_count == 1
    await lease.release()


async def test_proxy_deleted_after_candidate_selection_is_a_capacity_refusal(
    space: SpaceHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = await space.proxies.create("http://127.0.0.1:8932", group="g", max_identities=1)
    await space.proxies.configure_group("g", capacity_accounting="allocations", actor="test", reason="switch")
    identity = (
        await register(
            space, service="fake_tg", external_ref="owner", lifecycle="ready", session={"auth_key": "k"}
        )
    ).identity_id
    lease = await space.pools.define("p", service="fake_tg").acquire(among=[identity])
    original = PgTx.proxy_lock_rows
    deleted = False

    async def delete_before_lock(self, ids):
        nonlocal deleted
        if proxy.id in ids and not deleted:
            deleted = True
            await space.proxies.delete(proxy.id, actor="admin")
        await original(self, ids)

    monkeypatch.setattr(PgTx, "proxy_lock_rows", delete_before_lock)
    with pytest.raises(LeaseUnavailable):
        async with space._tx() as tx:
            await reserve(tx, identity, "g", owner_lease_id=lease.lease_id)
    async with space._tx() as tx:
        assert await tx.proxy_allocations(identity_id=identity) == []
        assert await tx.get_proxy(proxy.id) is None
    await lease.release()


async def test_public_deferred_acquire_competitors_commit_only_one_lease(
    space: SpaceHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = await space.proxies.create("http://127.0.0.1:8974", group="g", max_identities=1)
    await space.proxies.configure_group("g", capacity_accounting="allocations", actor="test", reason="enable")
    identities = [
        (
            await register(
                space, service="fake_tg", external_ref=ref, lifecycle="ready", session={"auth_key": "k"}
            )
        ).identity_id
        for ref in ("a", "b")
    ]
    pool = space.pools.define(
        "p", service="fake_tg", proxy_policy=ProxyPolicy(group="g", binding=ProxyBindingPolicy())
    )
    original = PgTx.proxy_lock_rows
    ready = asyncio.Event()
    arrived = set()

    async def barrier(self, ids):
        task = asyncio.current_task()
        name = task.get_name() if task else ""
        if proxy.id in ids and name.startswith("proxy-contender-") and name not in arrived:
            arrived.add(name)
            if len(arrived) == 2:
                ready.set()
            await ready.wait()
        await original(self, ids)

    monkeypatch.setattr(PgTx, "proxy_lock_rows", barrier)
    async with asyncio.timeout(10):
        results = await asyncio.gather(
            *(
                asyncio.create_task(
                    pool.acquire(among=[identity], idempotency_key=f"job-{n}"), name=f"proxy-contender-{n}"
                )
                for n, identity in enumerate(identities)
            ),
            return_exceptions=True,
        )
    assert len(arrived) == 2
    assert sum(isinstance(result, LeaseUnavailable) for result in results) == 1
    created = await space.admin.event_page(filters=EventFilters(type="proxy.reservation.created"))
    assert len(created.items) == 1
    async with space._tx() as tx:
        row = await tx.get_proxy(proxy.id)
        assert (row.bound_count, row.reserved_count, row.draining_count) == (0, 1, 0)
        for index, result in enumerate(results):
            if isinstance(result, BaseException):
                assert await tx.find_live_lease(f"job-{index}") is None
            else:
                assert (
                    await tx.find_live_lease(f"job-{index}")
                ).proxy_allocation_id == result.proxy_allocation_id
    for result in results:
        if not isinstance(result, BaseException):
            await result.release()
