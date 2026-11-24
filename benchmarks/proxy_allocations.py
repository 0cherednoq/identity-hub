"""PA5: сравнение HEAD/legacy/allocations на одинаковом локальном PostgreSQL.

Запускайте последовательно, без pytest на той же БД. Каждый запуск создаёт новую
схему ih_proxy_perf_<tag>_<variant> и оставляет её для проверки. URL берётся только
из IH_TEST_DATABASE_URL и никогда не записывается в отчёт. --source позволяет
загрузить исходный HEAD из git archive без изменения рабочего дерева.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import sys
import time
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def options() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", choices=("baseline", "legacy", "allocations"), required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--source", type=Path, default=ROOT / "src")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--iterations", type=int, default=25)
    parser.add_argument("--workers", type=int, default=50)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,19}", args.tag):
        parser.error("tag must match [a-z][a-z0-9_]{0,19}")
    if not 4 <= args.workers <= 50 or not 10 <= args.iterations <= 200:
        parser.error("workers: 4..50, iterations: 10..200")
    args.source = args.source.resolve()
    if not (args.source / "identity_hub").is_dir():
        parser.error("source must contain identity_hub")
    return args


async def run(args: argparse.Namespace) -> dict[str, Any]:
    # Выбор версии делается до любого импорта identity_hub, включая benchmark helpers.
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(args.source))
    from sqlalchemy import event, func, text, update
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from tests.fixtures.services import FAKE_TG

    import identity_hub as ih
    from benchmarks.run import environment, summarize
    from identity_hub.postgres.migrate import SCHEMA_HEAD, migrate
    from identity_hub.postgres.models import LeaseRecord

    assert Path(ih.__file__).is_relative_to(args.source), "wrong benchmark source"
    url = os.environ["IH_TEST_DATABASE_URL"]
    schema = f"ih_proxy_perf_{args.tag}_{args.variant}"
    application = schema
    engine = create_async_engine(
        url,
        pool_size=args.workers,
        max_overflow=0,
        pool_use_lifo=True,
        connect_args={"server_settings": {"application_name": application}},
    )
    observer = create_async_engine(url, pool_size=1, max_overflow=0, isolation_level="AUTOCOMMIT")
    active = []
    statements = 0
    locks = {"samples": 0, "waiting_peak": 0, "held_peak": 0}
    stop = asyncio.Event()

    def capture(*_args: Any) -> None:
        nonlocal statements
        statements += 1

    def stats(samples: list[float], sql: int, *, elapsed: float | None = None) -> dict[str, Any]:
        return {
            **asdict(summarize(samples, elapsed)),
            "sql_statements": sql,
            "sql_per_call": sql / len(samples),
        }

    async def monitor() -> None:
        async with observer.connect() as conn:
            while not stop.is_set():
                row = (
                    await conn.execute(
                        text(
                            "SELECT count(*) FILTER (WHERE NOT l.granted) AS waiting, "
                            "count(*) FILTER (WHERE l.granted) AS held "
                            "FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid "
                            "WHERE a.application_name=:app"
                        ),
                        {"app": application},
                    )
                ).one()
                locks["samples"] += 1
                locks["waiting_peak"] = max(locks["waiting_peak"], row.waiting)
                locks["held_peak"] = max(locks["held_peak"], row.held)
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), 0.01)

    async def acquire(pool: Any, identity: Any) -> Any:
        lease = await pool.acquire(among=[identity])
        active.append(lease)
        return lease

    async def cycles(pool: Any, identity: Any) -> dict[str, Any]:
        acquisitions, releases = [], []
        acquire_sql = release_sql = 0
        for index in range(args.iterations + 5):
            before, started = statements, time.perf_counter()
            lease = await acquire(pool, identity)
            elapsed, count = time.perf_counter() - started, statements - before
            if index >= 5:
                acquisitions.append(elapsed)
                acquire_sql += count
            before, started = statements, time.perf_counter()
            await lease.release()
            if index >= 5:
                releases.append(time.perf_counter() - started)
                release_sql += statements - before
        return {"acquire": stats(acquisitions, acquire_sql), "release": stats(releases, release_sql)}

    async with observer.connect() as conn:
        if (
            await conn.execute(text("SELECT 1 FROM pg_namespace WHERE nspname=:schema"), {"schema": schema})
        ).scalar():
            raise RuntimeError("benchmark schema already exists; use a new tag")
        deadlocks_before = (
            await conn.execute(
                text("SELECT deadlocks FROM pg_stat_database WHERE datname=current_database()")
            )
        ).scalar_one()
    await migrate(engine, db_schema=schema)
    hub = ih.IdentityHub(
        async_sessionmaker(engine, expire_on_commit=False),
        ih.StaticSecretsProvider({"bench": b"0123456789abcdef0123456789abcdef"}, "bench"),
        [FAKE_TG],
        db_schema=schema,
        secret_storage="encrypted",
    )
    await hub.start()
    space = hub.space("bench", create=True)
    ids = [
        identity.id
        for identity in (
            await space.identities.create_many(
                [
                    ih.IdentityCreate(
                        service=FAKE_TG.name,
                        external_ref=str(n),
                        lifecycle="ready",
                        session={"auth_key": "benchmark"},
                    )
                    for n in range(3 * args.workers + 4)
                ],
                actor="benchmark",
            )
        ).created
    ]
    await space.proxies.create("http://127.0.0.1:9080", group="g", max_identities=args.workers)
    strict = args.variant == "allocations"
    if strict:
        await space.proxies.configure_group(
            "g", capacity_accounting="allocations", actor="benchmark", reason="load"
        )

    def pool(name: str, group: str, *, temporary: bool = False) -> Any:
        kwargs: dict[str, Any] = {"group": group}
        if temporary:
            if strict:
                kwargs["binding"] = ih.ProxyBindingPolicy()
            else:
                kwargs["sticky"] = False
        return space.pools.define(
            name, service=FAKE_TG.name, holder="benchmark", proxy_policy=ih.ProxyPolicy(**kwargs)
        )

    steady = pool("steady", "g")
    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    monitoring = asyncio.create_task(monitor())
    report: dict[str, Any] = {
        "variant": args.variant,
        "source": str(args.source),
        "loaded_source": str(ih.__file__),
        "schema": schema,
        "schema_head": SCHEMA_HEAD,
        "environment": environment(),
        "workers": args.workers,
        "iterations": args.iterations,
        "profiles": {},
        "measurement": (
            "SQL statements exclude BEGIN/COMMIT; lock sampling interval >=10ms; "
            "no network provider I/O; concurrent throughput counts attempts over wall time"
        ),
    }
    try:
        held = await asyncio.gather(*(acquire(steady, identity) for identity in ids[1 : args.workers]))
        report["profiles"]["bound_at_full_capacity"] = await cycles(steady, ids[0])
        await space.proxies.unbind(ids[0], actor="benchmark", reason="reset target")
        temporary = pool("temporary", "g", temporary=True)
        report["profiles"]["temporary_at_high_occupancy"] = await cycles(temporary, ids[0])

        async def heartbeat(leases: list[Any]) -> dict[str, Any]:
            samples, sql = [], 0
            for index in range(args.iterations + 3):
                before, started = statements, time.perf_counter()
                renewed = await hub._store.renew_leases([lease._row.token for lease in leases], 120)
                assert len(renewed) == len(leases)
                assert all(row.expires_at is not None for row in renewed.values())
                if index >= 3:
                    samples.append(time.perf_counter() - started)
                    sql += statements - before
            return {**stats(samples, sql), "leases_per_batch": len(leases)}

        report["profiles"]["heartbeat_one"] = await heartbeat(held[:1])
        report["profiles"]["heartbeat_batch"] = await heartbeat(held)
        assert report["profiles"]["heartbeat_one"]["sql_per_call"] == 1
        assert report["profiles"]["heartbeat_batch"]["sql_per_call"] == 1

        competing = pool("contending", "g", temporary=strict)

        async def contend(identity: Any) -> tuple[float, Any]:
            started = time.perf_counter()
            try:
                result = await acquire(competing, identity)
            except ih.LeaseUnavailable as exc:
                result = exc
            return time.perf_counter() - started, result

        before, started = statements, time.perf_counter()
        attempts = await asyncio.gather(
            *(contend(identity) for identity in ids[args.workers : 2 * args.workers])
        )
        contention_elapsed = time.perf_counter() - started
        winners = [result for _, result in attempts if not isinstance(result, ih.LeaseUnavailable)]
        assert len(winners) == 1, f"last-slot winners: {len(winners)}"
        report["profiles"]["last_slot_contention"] = {
            **stats([sample for sample, _ in attempts], statements - before, elapsed=contention_elapsed),
            "winners": len(winners),
            "refusals": len(attempts) - len(winners),
        }
        for lease in winners:
            await lease.release()
            await space.proxies.unbind(lease.identity_id, actor="benchmark", reason="reset winner")
        await asyncio.gather(*(lease.release() for lease in held))

        async def new_group(name: str, port: int, capacity: int) -> Any:
            item = await space.proxies.create(f"http://127.0.0.1:{port}", group=name, max_identities=capacity)
            if strict:
                await space.proxies.configure_group(
                    name, capacity_accounting="allocations", actor="benchmark", reason="load"
                )
            return item

        recovering = await new_group("recovery", 9081, args.workers)
        recovery_pool = pool("recovery", "recovery", temporary=True)
        abandoned = await asyncio.gather(
            *(acquire(recovery_pool, identity) for identity in ids[args.workers : 2 * args.workers])
        )
        async with engine.begin() as conn:
            await conn.execute(
                update(LeaseRecord)
                .where(LeaseRecord.lease_id.in_([x.lease_id for x in abandoned]))
                .values(expires_at=func.now() - text("interval '1 second'")),
                execution_options={"schema_translate_map": {None: schema}},
            )
        recovery_results = {}
        # Пачки сравниваются в одном проходе накопленных expiry; число затронутых строк сохраняется.
        for batch in (8, 32):
            before, started = statements, time.perf_counter()
            result = await hub.maintenance._leases(batch)
            recovery_results[str(batch)] = {
                "elapsed_ms": (time.perf_counter() - started) * 1000,
                "sql_statements": statements - before,
                "counts": dict(result.counts),
            }
        for _ in range(math.ceil(args.workers / 8) + 2):
            await hub.maintenance._leases(8)
        recovery_card = (await space.admin.proxy_detail(recovering.id)).proxy
        assert recovery_card.in_lease_count == 0
        if strict:
            assert recovery_card.occupied_count == 0
        report["profiles"]["accumulated_expiry"] = recovery_results

        draining = await new_group("drain", 9082, 1)
        drain_pool = pool("drain", "drain")
        owner = await acquire(drain_pool, ids[-2])
        before, started = statements, time.perf_counter()
        await space.proxies.unbind(owner.identity_id, actor="benchmark", reason="drain")
        try:
            during = await acquire(drain_pool, ids[-1])
        except ih.LeaseUnavailable:
            during = None
        drain_ms, drain_sql = (time.perf_counter() - started) * 1000, statements - before
        assert (during is None) == strict
        if during is not None:
            await during.release()
            await space.proxies.unbind(during.identity_id, actor="benchmark", reason="reset legacy")
        await owner.release()
        after = await acquire(drain_pool, ids[-1])
        await after.release()
        report["profiles"]["drain"] = {
            "elapsed_ms": drain_ms,
            "sql_statements": drain_sql,
            "reissued_while_owner_live": during is not None,
            "returned_after_release": after.proxy_id == draining.id,
        }
        stop.set()
        await monitoring  # Return the observer's single connection before reading final statistics.
        async with observer.connect() as conn:
            report["indexes"] = [
                dict(row)
                for row in (
                    await conn.execute(
                        text(
                            "SELECT c.relname AS name, pg_relation_size(c.oid) AS bytes FROM pg_class c "
                            "JOIN pg_namespace n ON n.oid=c.relnamespace "
                            "WHERE n.nspname=:schema AND c.relkind='i' "
                            "ORDER BY c.relname"
                        ),
                        {"schema": schema},
                    )
                ).mappings()
            ]
            report["deadlocks"] = (
                await conn.execute(
                    text("SELECT deadlocks FROM pg_stat_database WHERE datname=current_database()")
                )
            ).scalar_one() - deadlocks_before
        report["index_bytes"] = sum(row["bytes"] for row in report["indexes"])
        report["locks"] = locks
        assert report["deadlocks"] == 0
    finally:
        stop.set()
        await monitoring
        await asyncio.gather(*(lease.release() for lease in active), return_exceptions=True)
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
        await engine.dispose()
        await observer.dispose()
    return report


if __name__ == "__main__":
    args = options()
    result = asyncio.run(run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(
        json.dumps(
            {"variant": args.variant, "schema_head": result["schema_head"], "output": str(args.output)}
        )
    )
