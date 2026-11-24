"""A stale proxy choice retries the transaction without consuming an identity candidate."""

import asyncio

import pytest

from identity_hub import Lease, LeaseUnavailable, ProxyBindingPolicy, ProxyPolicy, SpaceHandle
from identity_hub.lease import _CandidateBudget, acquire
from identity_hub.postgres.store import PgTx
from tests.fixtures.api import register

pytestmark = pytest.mark.pg


@pytest.mark.parametrize("managed", [False, True])
async def test_stale_choice_retries_another_proxy(
    space: SpaceHandle, monkeypatch: pytest.MonkeyPatch, managed: bool
) -> None:
    proxies = [
        await space.proxies.create(f"http://127.0.0.1:{8990 + n}", group="g", max_identities=2)
        for n in range(2)
    ]
    if managed:
        await space.proxies.configure_group(
            "g", capacity_accounting="allocations", actor="test", reason="enable"
        )
    identities = [
        (
            await register(
                space, service="fake_tg", external_ref=str(n), lifecycle="ready", session={"auth_key": "k"}
            )
        ).identity_id
        for n in range(5)
    ]
    pool = space.pools.define(
        "p",
        service="fake_tg",
        proxy_policy=ProxyPolicy(group="g", binding=ProxyBindingPolicy() if managed else None),
    )
    original = PgTx.choose_proxy
    arrived: set[str] = set()
    all_selected = asyncio.Event()

    async def choose(self, group, max_identities, *, claim, proxy_id=None):
        task = asyncio.current_task()
        name = task.get_name() if task else ""
        if not claim and name.startswith("proxy-retry-") and name not in arrived:
            # All workers see the same available proxy before any can lock it.
            selected = await self.get_proxy(proxies[0].id)
            arrived.add(name)
            if len(arrived) == 5:
                all_selected.set()
            await all_selected.wait()
            return selected
        return await original(self, group, max_identities, claim=claim, proxy_id=proxy_id)

    monkeypatch.setattr(PgTx, "choose_proxy", choose)
    async with asyncio.timeout(20):
        results = await asyncio.gather(
            *(
                asyncio.create_task(
                    acquire(pool, among=[identity], candidate_budget=_CandidateBudget(1)),
                    name=f"proxy-retry-{n}",
                )
                for n, identity in enumerate(identities)
            ),
            return_exceptions=True,
        )
    leases = [result for result in results if isinstance(result, Lease)]
    assert len(arrived) == 5
    assert len(leases) == 4, results
    assert sum(isinstance(result, LeaseUnavailable) for result in results) == 1
    async with space._tx() as tx:
        for proxy in proxies:
            row = await tx.get_proxy(proxy.id)
            assert (row.bound_count, row.reserved_count, row.draining_count) == (
                (0, 2, 0) if managed else (2, 0, 0)
            )
    for lease in leases:
        await lease.release()
