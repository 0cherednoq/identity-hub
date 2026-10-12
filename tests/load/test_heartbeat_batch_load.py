"""IMP-02 (I2.2b): продлений в секунду — поштучно против пачек 100 и 500; исключён из check.sh."""

from __future__ import annotations

import time

import pytest
from benchmarks.seed import seed_identities

from identity_hub.hub import IdentityHub
from identity_hub.service import OperationRule, Service

pytestmark = [pytest.mark.load, pytest.mark.pg, pytest.mark.timeout(900)]

LEASES = 500
ROUNDS = 5


async def test_batch_renew_throughput(hub: IdentityHub, db: object) -> None:
    hub.register_service(Service("hb", "HB", operations={"work": OperationRule()}))
    await hub.start()
    space = hub.space("hb", create=True)
    await seed_identities(db, space_id=(await space.ref()).id, service="hb", count=LEASES, prefix="hb")  # type: ignore[arg-type]
    pool = space.pools.define("hb", service="hb", holder="benchmark")
    leases = [await pool.acquire() for _ in range(LEASES)]
    tokens = [lease._row.token for lease in leases]
    store = hub._store

    rates: dict[str, float] = {}
    for label, size in (("single", 1), ("batch100", 100), ("batch500", 500)):
        started = time.perf_counter()
        for _ in range(ROUNDS):
            for offset in range(0, LEASES, size):
                renewed = await store.renew_leases(tokens[offset : offset + size], 60.0)
                assert len(renewed) == len(tokens[offset : offset + size])
        rates[label] = LEASES * ROUNDS / (time.perf_counter() - started)
    print("LOAD heartbeat renewals/s " + " ".join(f"{k}={v:.0f}" for k, v in rates.items()))
    assert rates["batch100"] > rates["single"] and rates["batch500"] > rates["single"]
    for lease in leases:
        await lease.release()
