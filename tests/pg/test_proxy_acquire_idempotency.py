"""PA2.1: старый ключ и истечение ключа не обходят снимок строгой выдачи."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from identity_hub import IdempotencyConflict, ProxyBindingPolicy, ProxyPolicy, SpaceHandle
from identity_hub.domain.idempotency import LEASE_FAMILY, fingerprint, namespace
from identity_hub.postgres.models import OperationKeyRecord
from identity_hub.proxy_issuance import request_base
from identity_hub.store.base import IdempotencyRequest
from tests.fixtures.api import register

pytestmark = pytest.mark.pg


async def _setup(space):
    proxy = await space.proxies.create("http://127.0.0.1:8971", group="g", max_identities=1)
    await space.proxies.create("http://127.0.0.1:8972", group="legacy")
    await space.proxies.configure_group("g", capacity_accounting="allocations", actor="test", reason="enable")
    identity = (
        await register(
            space, service="fake_tg", external_ref="a", lifecycle="ready", session={"auth_key": "k"}
        )
    ).identity_id
    return proxy, identity


@pytest.mark.parametrize("version", [0, 1])
@pytest.mark.parametrize("outcome", ["failed", "succeeded"])
@pytest.mark.parametrize("primary_strict", [True, False])
async def test_incompatible_old_key_cannot_issue_managed_proxy(
    space: SpaceHandle, sessionmaker, version, outcome, primary_strict
) -> None:
    proxy, identity = await _setup(space)
    pool = space.pools.define(
        "p",
        service="fake_tg",
        holder="worker",
        proxy_policy=ProxyPolicy(
            group="g" if primary_strict else "legacy",
            binding=ProxyBindingPolicy() if primary_strict else None,
        ),
    )
    if not primary_strict:
        await space.proxies.rebind(identity, proxy.id, actor="admin", reason="trusted")
    key_namespace = namespace((await space.ref()).id, LEASE_FAMILY)
    payload = {"pool": "p", "service": "fake_tg", "holder": "worker"}
    async with space._tx() as tx:
        if version == 0 and primary_strict:
            # Даже совпавший hash version=0 не доказывает состав новой команды.
            payload["proxy_request"] = request_base(pool, await tx.proxy_group("g"))
        await tx.claim_idempotency_key(
            IdempotencyRequest(
                key_namespace, "old", LEASE_FAMILY, fingerprint(LEASE_FAMILY, payload), timedelta(days=1)
            )
        )
        await tx.complete_idempotency_key(
            key_namespace, "old", outcome, result_type=LEASE_FAMILY, result_version=1
        )
    async with sessionmaker.begin() as session:
        await session.execute(
            update(OperationKeyRecord)
            .where(OperationKeyRecord.namespace == key_namespace, OperationKeyRecord.key == "old")
            .values(fingerprint_version=version)
        )
    with pytest.raises(IdempotencyConflict):
        await pool.acquire(among=[identity], idempotency_key="old")
    async with space._tx() as tx:
        assert await tx.current_lease(identity) is None
        assert (await tx.get_proxy(proxy.id)).reserved_count == 0


async def test_expired_key_replay_uses_durable_lease_request(space: SpaceHandle, sessionmaker) -> None:
    proxy, identity = await _setup(space)
    pool = space.pools.define(
        "p",
        service="fake_tg",
        holder="worker",
        proxy_policy=ProxyPolicy(group="g", binding=ProxyBindingPolicy()),
    )
    first = await pool.acquire(among=[identity], idempotency_key="job")
    key_namespace = namespace((await space.ref()).id, LEASE_FAMILY)

    async def expire_key():
        async with sessionmaker.begin() as session:
            await session.execute(
                update(OperationKeyRecord)
                .where(OperationKeyRecord.namespace == key_namespace, OperationKeyRecord.key == "job")
                .values(expires_at=func.now() - timedelta(seconds=1))
            )

    await expire_key()
    again = await pool.acquire(idempotency_key="job")
    assert (again.lease_id, again.proxy_allocation_id) == (first.lease_id, first.proxy_allocation_id)
    async with sessionmaker() as session:
        result = (
            await session.execute(
                select(OperationKeyRecord.result).where(
                    OperationKeyRecord.namespace == key_namespace, OperationKeyRecord.key == "job"
                )
            )
        ).scalar_one()
        assert result["proxy_request"] == first._row.proxy_request
    await expire_key()
    changed = space.pools.define(
        "p",
        service="fake_tg",
        holder="worker",
        proxy_policy=ProxyPolicy(group="g", binding=ProxyBindingPolicy(), max_identities=1),
    )
    with pytest.raises(IdempotencyConflict):
        await changed.acquire(idempotency_key="job")
    async with space._tx() as tx:
        assert (await tx.get_proxy(proxy.id)).reserved_count == 1
    await first.release()


async def test_untyped_receipt_does_not_replay_managed_use_through_legacy_policy(
    space: SpaceHandle, sessionmaker
) -> None:
    proxy, identity = await _setup(space)
    await space.proxies.rebind(identity, proxy.id, actor="admin", reason="trusted")
    pool = space.pools.define(
        "p", service="fake_tg", holder="worker", proxy_policy=ProxyPolicy(group="legacy")
    )
    lease = await pool.acquire(among=[identity], idempotency_key="job")
    key_namespace = namespace((await space.ref()).id, LEASE_FAMILY)
    async with sessionmaker.begin() as session:
        await session.execute(
            update(OperationKeyRecord)
            .where(OperationKeyRecord.namespace == key_namespace, OperationKeyRecord.key == "job")
            .values(result_version=1)
        )
    with pytest.raises(IdempotencyConflict):
        await pool.acquire(idempotency_key="job")
    async with space._tx() as tx:
        await tx.check_fence(lease.fence)
        assert (await tx.get_proxy(proxy.id)).bound_count == 1
    await lease.release()
