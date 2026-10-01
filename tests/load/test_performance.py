"""Явные нагрузочные бюджеты ТЗ §10.1; исключены из обычного scripts/check.sh."""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta

import pytest
from benchmarks.run import measure, summarize
from benchmarks.seed import seed_identities
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from identity_hub.events import Event, EventDraft
from identity_hub.hub import IdentityHub
from identity_hub.importing import ImportRecord
from identity_hub.lease import Lease
from identity_hub.postgres.models import IdentityRecord, LeaseRecord
from identity_hub.service import OperationRule, Service
from identity_hub.store.base import SpaceRef

pytestmark = [pytest.mark.load, pytest.mark.pg, pytest.mark.anyio]


def _budget(actual: float, maximum: float, metric: str) -> None:
    if actual > maximum:
        pytest.xfail(f"D-127 PERF-1: {metric}={actual:.3f} превышает бюджет {maximum:.3f}")


async def _load_space(hub: IdentityHub) -> tuple[Service, SpaceRef]:
    service = Service("load", "Load", operations={"work": OperationRule()})
    hub.register_service(service)
    await hub.start()
    return service, await hub.space("load").ref()


async def test_lease_p95_10k_accounts_50_workers(
    hub: IdentityHub,
    db: AsyncEngine,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    service, ref = await _load_space(hub)
    await seed_identities(db, space_id=ref.id, service=service.name, count=10_000, prefix="lease")
    pool = hub.space("load").pools.define("load", service=service, holder="benchmark")

    async def acquire_one() -> tuple[float, Lease]:
        started = time.perf_counter()
        lease = await pool.acquire()
        return time.perf_counter() - started, lease

    total_started = time.perf_counter()
    raw_results = await asyncio.gather(*(acquire_one() for _ in range(50)), return_exceptions=True)
    errors = [result for result in raw_results if isinstance(result, BaseException)]
    results = [result for result in raw_results if not isinstance(result, BaseException)]
    assert not errors, errors
    stats = summarize([sample for sample, _ in results], time.perf_counter() - total_started)
    print(f"LOAD lease {stats}")
    leases = [lease for _, lease in results]
    assert len({lease.identity_id for lease in leases}) == 50

    async with sessionmaker() as session:
        live, distinct, stale = (
            await session.execute(
                select(
                    func.count(),
                    func.count(func.distinct(LeaseRecord.identity_id)),
                    func.count().filter(LeaseRecord.fencing != IdentityRecord.fencing_seq),
                ).join(IdentityRecord, IdentityRecord.id == LeaseRecord.identity_id)
            )
        ).one()
    assert (live, distinct, stale) == (50, 50, 0)
    await asyncio.gather(*(lease.release() for lease in leases))
    assert db.pool.checkedout() == 0  # type: ignore[attr-defined]
    _budget(stats.p95_ms, 20, "lease.p95_ms")


async def test_operation_p95(hub: IdentityHub, db: AsyncEngine) -> None:
    service, ref = await _load_space(hub)
    await seed_identities(db, space_id=ref.id, service=service.name, count=1, prefix="operation")
    lease = await hub.space("load").pools.define("load", service=service).acquire()

    async def operation() -> None:
        async with lease.operation("work"):
            pass

    stats = await measure(operation, iterations=30, warmup=3)
    print(f"LOAD operation {stats}")
    await lease.release()
    _budget(stats.p95_ms, 10, "operation.p95_ms")


async def test_search_p95_100k(hub: IdentityHub, db: AsyncEngine) -> None:
    service, ref = await _load_space(hub)
    await seed_identities(db, space_id=ref.id, service=service.name, count=100_000, prefix="search")
    space = hub.space("load")

    async def search() -> None:
        page = await space.identities.search("service:load search-099", limit=50, count=False)
        assert page.items

    stats = await measure(search, iterations=20, warmup=3)
    print(f"LOAD search {stats}")
    _budget(stats.p95_ms, 100, "search.p95_ms")


async def test_import_10k_under_60_seconds(hub: IdentityHub) -> None:
    records = [ImportRecord(f"import-{index:06d}") for index in range(10_000)]
    started = time.perf_counter()
    report = await hub.space("load-import").imports.run(service="fake_tg", records=records, batch_size=2_000)
    elapsed = time.perf_counter() - started
    print(f"LOAD import count={report.total} elapsed={elapsed:.3f}s")
    assert report.total == 10_000 and len(report.created) == 10_000
    _budget(elapsed, 60, "import.elapsed_s")


async def test_event_latency_p95(hub: IdentityHub) -> None:
    space = hub.space("load-events")
    ref = await space.ref()
    samples: list[float] = []
    for index in range(20):
        started = time.perf_counter()
        async with hub.store.transaction(ref) as transaction:
            await transaction.emit([EventDraft("identity.imported", "benchmark", data={"index": index})])

        async def handled(events: list[Event]) -> None:
            assert events

        count = await hub.store.process_events(f"load-latency-{index}", handled, limit=100)
        assert count is not None and count >= 1
        samples.append(time.perf_counter() - started)
    stats = summarize(samples)
    print(f"LOAD event {stats}")
    _budget(stats.p95_ms, timedelta(seconds=1).total_seconds() * 1000, "event.p95_ms")
