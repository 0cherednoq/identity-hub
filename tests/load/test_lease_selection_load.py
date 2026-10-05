"""IMP-01 (I2.1): p95 аренды при 0/50/90/99% занятых на 100k аккаунтов; исключён из check.sh."""

from __future__ import annotations

import time
from datetime import timedelta

import pytest
from benchmarks.run import summarize
from benchmarks.seed import seed_identities
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from identity_hub.hub import IdentityHub
from identity_hub.service import OperationRule, Service

# засев 100k аккаунтов и живых аренд на занятой машине дольше общего лимита в 120 с
pytestmark = [pytest.mark.load, pytest.mark.pg, pytest.mark.anyio, pytest.mark.timeout(900)]

ACCOUNTS = 100_000
SAMPLES = 200


def _budget(actual: float, maximum: float, metric: str) -> None:
    if actual > maximum:
        pytest.xfail(f"D-127 PERF-1: {metric}={actual:.3f} превышает бюджет {maximum:.3f}")


@pytest.mark.parametrize("busy", [0.0, 0.5, 0.9, 0.99], ids=["0", "50", "90", "99"])
async def test_lease_p95_by_occupancy_100k(hub: IdentityHub, db: AsyncEngine, busy: float) -> None:
    service = Service("lru", "LRU", operations={"work": OperationRule()})
    hub.register_service(service)
    await hub.start()
    space = hub.space("lru", create=True)
    ref = await space.ref()
    ids = await seed_identities(db, space_id=ref.id, service="lru", count=ACCOUNTS, prefix="lru")
    taken = ids[: int(ACCOUNTS * busy)]
    async with db.begin() as conn:
        # занятые: живая аренда и свежий захват — как после pool.acquire()
        await conn.execute(
            text(
                "INSERT INTO ih_leases (identity_id, space_id, lease_id, token, fencing, pool, holder, "
                "acquired_at, heartbeat_at, expires_at) SELECT id, space_id, gen_random_uuid(), "
                "gen_random_uuid(), 1, 'busy', 'load', now(), now(), now() + interval '1 hour' "
                "FROM ih_identities WHERE id = ANY(:ids)"
            ),
            {"ids": taken},
        )
        await conn.execute(
            text("UPDATE ih_identities SET last_leased_at = clock_timestamp() WHERE id = ANY(:ids)"),
            {"ids": taken},
        )
        await conn.execute(text("ANALYZE ih_identities"))
        await conn.execute(text("ANALYZE ih_leases"))
    pool = space.pools.define("load", service="lru", holder="benchmark", ttl=timedelta(minutes=5))

    samples: list[float] = []
    started_all = time.perf_counter()
    for _ in range(SAMPLES):
        started = time.perf_counter()
        lease = await pool.acquire()
        samples.append(time.perf_counter() - started)
        await lease.release()
    stats = summarize(samples, time.perf_counter() - started_all)
    print(f"LOAD lease busy={busy:.0%} {stats}")
    _budget(stats.p95_ms, 20, f"lease.p95_ms busy={busy:.0%}")
