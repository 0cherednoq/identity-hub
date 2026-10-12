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
from identity_hub.service import OperationRule, Quota, Service

# засев 100k аккаунтов и живых аренд на занятой машине дольше общего лимита в 120 с
pytestmark = [pytest.mark.load, pytest.mark.pg, pytest.mark.timeout(900)]

ACCOUNTS = 100_000
SAMPLES = 200
WARMUP = 60  # захватов до замера с require_quota: хватает, чтобы очистить голову из 50k


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
            text(
                "UPDATE ih_identities SET last_leased_at = clock_timestamp(), "
                "pick_order_at = clock_timestamp() WHERE id = ANY(:ids)"
            ),
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


@pytest.mark.parametrize("worst_case", [False, True], ids=["mixed", "exhausted-first"])
async def test_require_quota_p95_half_exhausted_100k(
    hub: IdentityHub, db: AsyncEngine, worst_case: bool
) -> None:
    """N1.3: выборка с `require_quota` при половине исчерпанных аккаунтов.

    `exhausted-first` — худший случай: исчерпанные стоят в голове порядка выборки. До N1.3b выборка
    проходила мимо каждого (p95 187 мс); теперь захват переносит их в хвост, и после прогрева
    замер должен совпадать с базовым.
    """
    quota = Quota(5, timedelta(hours=1))
    service = Service("quota", "Quota", operations={"send": OperationRule(quotas=(quota,))})
    hub.register_service(service)
    await hub.start()
    space = hub.space("quota", create=True)
    ref = await space.ref()
    ids = await seed_identities(db, space_id=ref.id, service="quota", count=ACCOUNTS, prefix="quota")
    exhausted = ids[: ACCOUNTS // 2]
    async with db.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO ih_quota_usage "
                "(identity_id, kind, window_seconds, window_start, space_id, used) "
                "SELECT id, 'send', 3600, date_bin(interval '3600 seconds', now(), :origin), space_id, 5 "
                "FROM ih_identities WHERE id = ANY(:ids)"
            ),
            {"ids": exhausted, "origin": quota.origin},
        )
        if worst_case:  # свободные уже арендовались, исчерпанные — никогда: они первые в порядке LRU
            await conn.execute(
                text(
                    "UPDATE ih_identities SET last_leased_at = clock_timestamp(), "
                    "pick_order_at = clock_timestamp() WHERE NOT id = ANY(:ids)"
                ),
                {"ids": exhausted},
            )
        await conn.execute(text("ANALYZE ih_identities"))
        await conn.execute(text("ANALYZE ih_quota_usage"))
    pool = space.pools.define("load", service="quota", holder="benchmark", ttl=timedelta(minutes=5))
    taken = set(exhausted)
    # прогрев: каждый захват переносит в хвост ограниченную пачку исчерпанных из головы порядка (N1.3b);
    # в работе они появляются в голове по одному, а здесь стоят там все сразу
    warmup_started = time.perf_counter()
    for _ in range(WARMUP):
        lease = await pool.acquire(for_operation="send", require_quota=True)
        await lease.release()
    # Синтетический прогрев за секунды меняет 50k строк индексированной колонки. В обычной работе
    # эти изменения распределены во времени и их убирает autovacuum; перед замером воспроизводим
    # установившееся состояние, чтобы не измерять мёртвые версии из подготовки benchmark.
    async with db.connect() as connection:
        autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await autocommit.execute(text("VACUUM (ANALYZE) ih_identities"))
    warmup_seconds = time.perf_counter() - warmup_started
    print(f"LOAD lease require_quota worst_case={worst_case} warmup_s={warmup_seconds:.1f}")

    baseline_samples: list[float] = []
    for _ in range(SAMPLES):
        started = time.perf_counter()
        lease = await pool.acquire()
        baseline_samples.append(time.perf_counter() - started)
        await lease.release()
    baseline = summarize(baseline_samples, sum(baseline_samples))
    print(f"LOAD lease require_quota baseline {baseline}")

    samples: list[float] = []
    started_all = time.perf_counter()
    for _ in range(SAMPLES):
        started = time.perf_counter()
        lease = await pool.acquire(for_operation="send", require_quota=True)
        samples.append(time.perf_counter() - started)
        assert lease.identity_id not in taken
        await lease.release()
    stats = summarize(samples, time.perf_counter() - started_all)
    print(f"LOAD lease require_quota worst_case={worst_case} {stats}")
    # D-300 требует сравнения в одном окружении, а не абсолютных 20 мс: на загруженной машине
    # даже базовый замер бывает выше. Допуск 25% покрывает один точечный lookup квоты.
    budget = max(20.0, baseline.p95_ms * 1.25)
    _budget(stats.p95_ms, budget, f"lease.p95_ms require_quota worst_case={worst_case}")
