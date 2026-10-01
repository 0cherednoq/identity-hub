"""E4/E5: 0008 на непустой БД (время, память, блокировка), 0004 без backfill bound_count."""

from __future__ import annotations

import asyncio
import time
import tracemalloc

import asyncpg
from sqlalchemy import text

from identity_hub.postgres.migrate import migrate
from sch_common import PORT, fresh_db, run

N = 300_000


async def main() -> None:
    eng = await fresh_db("sch_e4")
    await migrate(eng, revision="0003")
    async with eng.begin() as conn:
        await conn.execute(text("insert into ih_spaces(id, slug, title) values (gen_random_uuid(), 's', 's')"))
        await conn.execute(text("insert into ih_services(name, title) values ('svc', 'svc')"))
        await conn.execute(
            text(
                """
            insert into ih_identities(id, space_id, service, external_ref, lifecycle, standing, auth_state,
                                      availability, error_score, fencing_seq, event_seq)
            select gen_random_uuid(), (select id from ih_spaces), 'svc', ' ref-' || g || ' ', 'ready', 'good',
                   'authenticated', 'available', 0, 0, 0
            from generate_series(1, :n) g
            """
            ),
            {"n": N},
        )
        # прокси с двумя привязками до появления bound_count (0004)
        await conn.execute(
            text(
                """
            insert into ih_proxies(id, space_id, pool, scheme, host, port, fingerprint, status, error_score,
                                   fail_streak, status_source, max_identities, key_id, wrapped_dek, nonce, ciphertext)
            values (gen_random_uuid(), (select id from ih_spaces), 'p', 'http', 'h', 1, 'f', 'alive', 0, 0,
                    'command', 3, 'k', ''::bytea, ''::bytea, ''::bytea)
            """
            )
        )
        await conn.execute(
            text(
                """
            insert into ih_bindings(identity_id, space_id, proxy_id)
            select id, space_id, (select id from ih_proxies) from ih_identities limit 2
            """
            )
        )
    await migrate(eng, revision="0007")
    async with eng.connect() as conn:
        print(
            "E5 after 0004: bound_count vs real bindings:",
            (
                await conn.execute(
                    text("select bound_count, (select count(*) from ih_bindings) from ih_proxies")
                )
            ).all(),
        )

    # параллельный читатель: сколько времени обычный SELECT по ih_identities висит на блокировке
    blocked: list[float] = []
    stop = asyncio.Event()

    async def reader() -> None:
        conn = await asyncpg.connect(f"postgresql://postgres:review@127.0.0.1:{PORT}/sch_e4")
        try:
            while not stop.is_set():
                t = time.perf_counter()
                await conn.fetchval("select count(*) from (select 1 from ih_identities limit 1) q")
                blocked.append(time.perf_counter() - t)
                await asyncio.sleep(0.05)
        finally:
            await conn.close()

    task = asyncio.create_task(reader())
    await asyncio.sleep(0.3)
    tracemalloc.start()
    t0 = time.perf_counter()
    await migrate(eng)
    elapsed = time.perf_counter() - t0
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    stop.set()
    await task
    print(f"E4 0008 on {N} rows: {elapsed:.1f}s, python peak {peak / 1e6:.0f} MB, max reader stall {max(blocked):.1f}s")
    async with eng.connect() as conn:
        print(
            "dead tuples / size:",
            (
                await conn.execute(
                    text(
                        "select n_dead_tup, pg_size_pretty(pg_total_relation_size('ih_identities')) "
                        "from pg_stat_user_tables where relname='ih_identities'"
                    )
                )
            ).all(),
        )
    await eng.dispose()


if __name__ == "__main__":
    run(main())
