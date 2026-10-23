"""I6.4 (`IMP-17`): аренда при concurrency 1/4/8/16/32/64 — где именно теряется время."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from benchmarks.run import summarize
from benchmarks.seed import seed_identities
from sqlalchemy.ext.asyncio import AsyncEngine

from identity_hub import IdentityHub, OperationRule, Service

pytestmark = pytest.mark.load

LEVELS = (1, 4, 8, 16, 32, 64)


async def test_lease_latency_by_concurrency(hub: IdentityHub, db: AsyncEngine) -> None:
    service = Service("ladder", "Ladder", operations={"work": OperationRule()})
    hub.register_service(service)
    await hub.start()
    space = hub.space("ladder", create=True)
    ref = await space.ref()
    await seed_identities(db, space_id=ref.id, service=service.name, count=2_000, prefix="ladder")
    pool = space.pools.define("ladder", service=service, holder="benchmark")

    async def once() -> tuple[float, Any]:
        started = time.perf_counter()
        lease = await pool.acquire()
        return time.perf_counter() - started, lease

    report: dict[int, Any] = {}
    for level in LEVELS:
        results = await asyncio.gather(*(once() for _ in range(level)))
        stats = summarize([sample for sample, _ in results])
        report[level] = stats
        print(f"LOAD lease concurrency={level} {stats}")
        await asyncio.gather(*(lease.release() for _, lease in results))

    assert report[1].p95_ms > 0
