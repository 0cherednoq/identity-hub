"""PA3: массовая проекция состояния и admin rebind соблюдают общий порядок блокировок."""

import asyncio
from dataclasses import replace

import pytest

from identity_hub import IdentityCreate, Override, ProxyBindingPolicy, ProxyPolicy, RegionIn
from identity_hub.postgres.store import PgTx
from tests.fixtures.services import FAKE_TG

pytestmark = pytest.mark.pg


async def test_state_batch_and_crossed_rebind_take_whole_proxy_set(hub, space, monkeypatch):
    service = replace(
        FAKE_TG,
        name="proxy_state_batch",
        proxy_policy=ProxyPolicy(
            group="g", binding=ProxyBindingPolicy(unbind_when=RegionIn("standing", states={"banned"}))
        ),
    )
    hub.register_service(service)
    await hub.start()
    proxies = [
        await space.proxies.create(f"http://127.0.0.1:{8993 + n}", group="g", max_identities=2)
        for n in range(2)
    ]
    await space.proxies.configure_group("g", capacity_accounting="allocations", actor="test", reason="enable")
    identities = [
        (
            await space.identities.create(
                IdentityCreate(
                    service=service.name,
                    external_ref=str(n),
                    lifecycle="ready",
                    session={"auth_key": "k"},
                ),
                actor="test",
            )
        ).id
        for n in range(2)
    ]
    for identity, proxy in zip(identities, proxies, strict=True):
        await space.proxies.rebind(identity, proxy.id, actor="admin", reason="initial")
    original = PgTx.lock_identities
    ready = asyncio.Event()
    arrivals = set()

    async def lock(tx, ids):
        name = asyncio.current_task().get_name()
        if name in {"invalidate", "replace"} and name not in arrivals:
            assert set(ids) == set(identities)
            arrivals.add(name)
            if len(arrivals) == 2:
                ready.set()
            await ready.wait()
        await original(tx, ids)

    monkeypatch.setattr(PgTx, "lock_identities", lock)
    async with asyncio.timeout(10):
        results = await asyncio.gather(
            asyncio.create_task(
                space.identities.override_many(
                    reversed(identities),
                    Override("standing", None, "banned", actor="checker", reason="invalid"),
                    atomic=True,
                ),
                name="invalidate",
            ),
            asyncio.create_task(
                space.proxies.rebind_many(
                    {identities[0]: proxies[1].id, identities[1]: proxies[0].id},
                    actor="admin",
                    reason="replace",
                    atomic=True,
                ),
                name="replace",
            ),
        )
    assert arrivals == {"invalidate", "replace"}
    assert all(not result.rejected and set(result.applied) == set(identities) for result in results)
    async with space._tx() as tx:
        bindings = await tx.proxy_bindings(group="g")
        # Оба порядка допустимы: оператор вправе явно создать доверенную пару
        # после принятой команды состояния, а обратный порядок снимает новую пару.
        assert len(bindings) in {0, 2}
        for proxy in proxies:
            row = await tx.get_proxy(proxy.id)
            assert row.bound_count == sum(binding.proxy_id == proxy.id for binding in bindings)
            assert row.reserved_count == row.draining_count == 0
        for identity in identities:
            assert (await tx.get_identity(identity)).standing == "banned"
