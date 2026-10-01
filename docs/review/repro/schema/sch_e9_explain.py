"""E9/E13: планы и время горячих запросов на 300k identities (база sch_e4, public)."""

from __future__ import annotations

import re
import sys
import time
from datetime import timedelta

import asyncpg
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from identity_hub.crypto import StaticSecretsProvider
from identity_hub.domain.query import parse_query
from identity_hub.hub import IdentityHub
from identity_hub.read.search import AccountSearchOptions
from identity_hub.service import Service
from sch_common import PORT, run, url

SVC = Service(name="svc", title="svc")
CAPTURED: list[tuple[str, tuple]] = []
ONLY = set(sys.argv[1:])

SEED = [
    "analyze ih_identities",
    """insert into ih_region_states(identity_id, region, key, space_id, state, change_source, version, synced_at, until)
       select id, r.region, r.key, space_id, r.state, 'command', 1, now() - random() * interval '3 days',
              case when random() < 0.001 then now() - interval '1 minute' end
       from ih_identities cross join (values ('standing','','good'), ('auth','default','authenticated')) r(region,key,state)
       on conflict do nothing""",
    """insert into ih_leases(identity_id, space_id, lease_id, token, fencing, pool, holder, expires_at)
       select id, space_id, gen_random_uuid(), gen_random_uuid(), 1, 'p', 'h', now() - random() * interval '30 days'
       from ih_identities on conflict do nothing""",
    """insert into ih_lease_history(lease_id, space_id, identity_id, token, fencing, pool, holder, succeeded, failed,
                                    acquired_at, released_at, end_reason)
       select lease_id, space_id, identity_id, token, 1, 'p', 'h', 0, 0, expires_at - interval '1 minute', expires_at,
              'released' from ih_leases on conflict do nothing""",
    """insert into ih_events(event_id, type, severity, space_id, service, identity_id, actor, external, occurred_at)
       select gen_random_uuid(), 'identity.updated', 'info', (select id from ih_spaces), 'svc', null, 'a', true,
              now() - random() * interval '200 days' from generate_series(1, 500000)""",
    """insert into ih_quota_usage(identity_id, kind, window_seconds, window_start, space_id, used)
       select id, 'op', 3600, date_trunc('hour', now() - random() * interval '20 hours'), space_id, 1 from ih_identities
       on conflict do nothing""",
    """insert into ih_webhook_endpoints(id, space_id, url, event_types, min_severity, enabled, failure_streak, key_id,
                                        wrapped_dek, nonce, ciphertext)
       select gen_random_uuid(), (select id from ih_spaces), 'https://x/' || g, '{*}', 'info', g = 2, 0, 'k', '', '', ''
       from generate_series(1, 2) g""",
    """insert into ih_webhook_deliveries(endpoint_id, event_id, status, attempt, next_attempt_at, created_at)
       select (select id from ih_webhook_endpoints where not enabled), event_id, 'pending', 0,
              now() - interval '1 day', now() - random() * interval '40 days' from ih_events limit 200000""",
    "analyze",
]


def want(name: str) -> bool:
    return not ONLY or name in ONLY


async def explain(raw: asyncpg.Connection, label: str, analyze: bool = True) -> None:
    for stmt, params in CAPTURED:
        head = "EXPLAIN (ANALYZE, TIMING OFF, COSTS OFF, SUMMARY ON) " if analyze else "EXPLAIN (COSTS OFF) "
        tr = raw.transaction()
        await tr.start()
        try:
            rows = await raw.fetch(head + stmt, *params)
        except Exception as exc:
            print(f"  [{label}] explain failed: {type(exc).__name__} {str(exc)[:160]}")
            await tr.rollback()
            continue
        await tr.rollback()
        lines = [r[0] for r in rows]
        scans = [l.strip() for l in lines if re.search(r"Seq Scan|Index|Sort|Bitmap|Execution Time|Limit|Delete", l)]
        short = re.sub(r"\s+", " ", stmt)[:110]
        print(f"  [{label}] {short}")
        for l in scans[:9]:
            print("      ", l[:170])
    CAPTURED.clear()


async def timed(label: str, coro):  # type: ignore[no-untyped-def]
    t = time.perf_counter()
    try:
        result = await coro
        print(f"{label}: {time.perf_counter() - t:.2f}s")
        return result
    except Exception as exc:
        print(f"{label}: EXC {type(exc).__name__} {str(exc)[:200]}")


async def main() -> None:
    engine = create_async_engine(url("sch_e4"), pool_size=5)
    raw = await asyncpg.connect(f"postgresql://postgres:review@127.0.0.1:{PORT}/sch_e4")
    seeded = await raw.fetchval("select count(*) from ih_events")
    if not seeded:
        for s in SEED:
            await raw.execute(s)
        print("seeded")

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _capture(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        if "set_config" in statement or statement.startswith("SELECT now()"):
            return
        CAPTURED.append((statement, tuple(parameters) if parameters else ()))

    hub = IdentityHub(async_sessionmaker(engine, expire_on_commit=False),
                      StaticSecretsProvider({"k1": b"\x11" * 32}, "k1"), [SVC])
    await hub.start()
    space = hub.space("s")
    await space.ref()
    CAPTURED.clear()

    if want("search"):
        for label, opts in (
            ("created_at desc (default list)", AccountSearchOptions()),
            ("created_at asc", AccountSearchOptions(sort_desc=False)),
            ("external_ref asc", AccountSearchOptions(sort_by="external_ref", sort_desc=False)),
            ("last_leased_at desc", AccountSearchOptions(sort_by="last_leased_at")),
        ):
            flt = space._search_filter(parse_query(""), opts)
            async with space._tx(own=True) as tx:
                rows, _ = await timed(f"search {label} p1", tx.search_identities(flt, limit=51, after=None, count_limit=1000))
            await explain(raw, label)
        flt = space._search_filter(parse_query("ref-12345"), AccountSearchOptions())
        async with space._tx(own=True) as tx:
            await timed("search term 'ref-12345'", tx.search_identities(flt, limit=51, after=None, count_limit=1000))
        await explain(raw, "term")
        flt = space._search_filter(parse_query("id:abcd"), AccountSearchOptions())
        async with space._tx(own=True) as tx:
            await timed("search id:abcd (short id)", tx.search_identities(flt, limit=51, after=None, count_limit=1000))
        CAPTURED.clear()
        t = time.perf_counter()
        page = await space._account_search("", limit=100)
        print(f"account_search page of 100: {time.perf_counter() - t:.2f}s, SQL statements: {len(CAPTURED)}")
        CAPTURED.clear()

    if want("maint"):
        async with space._tx(own=True) as tx:
            await timed("expire_leases(500)", tx.expire_leases(500))
        print("  statements in one expire_leases tick:", len(CAPTURED))
        del CAPTURED[1:]
        await explain(raw, "expire_leases")
        async with space._tx(own=True) as tx:
            await timed("due_regions('standing')", tx.due_regions("standing", 500))
            await timed("due_identity_timers", tx.due_identity_timers(500))
            await timed("stale_identities", tx.stale_identities("svc", "standing", "", 3600, 100))
        await explain(raw, "timers")
        await timed("claim_webhook_deliveries", hub.store.claim_webhook_deliveries(limit=50, claim_for=timedelta(minutes=1)))
        del CAPTURED[1:]
        await explain(raw, "claim_webhooks")
        await timed("metrics_snapshot", hub.store.metrics_snapshot())
        CAPTURED.clear()
        async with space._tx(own=True) as tx:
            await timed("overview_snapshot", tx.overview_snapshot())
        CAPTURED.clear()
        async with space._tx(own=True) as tx:
            await timed("webhook_delivery_read_page(50)", tx.webhook_delivery_read_page(limit=50))
        await explain(raw, "delivery page")
        await timed("capture_state_snapshot", hub.state_snapshots.capture())
        CAPTURED.clear()

    if want("cleanup"):
        retention = hub.maintenance._retention()
        names = frozenset({"quota_windows", "operation_keys", "lease_logs", "lease_history", "webhook_deliveries",
                           "notifications", "events", "import_runs", "provider_operations"})
        # только планы: сам cleanup не выполняем, чтобы не менять данные между прогонами
        from identity_hub.postgres import store as st
        from sqlalchemy import delete, func
        from sqlalchemy.dialects import postgresql

        stmts = {
            "quota_windows": delete(st.QUOTAS).where(
                st.QUOTAS.c.window_start + func.make_interval(0, 0, 0, 0, 0, 0, st.QUOTAS.c.window_seconds) <= func.now()),
            "events": delete(st.EVENTS).where(st.EVENTS.c.occurred_at < func.now() - retention["events"]),
            "lease_history": delete(st.HISTORY).where(
                st.HISTORY.c.released_at.is_not(None), st.HISTORY.c.released_at < func.now() - retention["lease_history"]),
            "webhook_deliveries": delete(st.WEBHOOK_DELIVERIES).where(
                st.WEBHOOK_DELIVERIES.c.created_at < func.now() - retention["deliveries"]),
        }
        for name, stmt in stmts.items():
            sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
            await raw.execute("set enable_seqscan = off")
            plan_noseq = [r[0] for r in await raw.fetch("EXPLAIN (COSTS OFF) " + sql)]
            await raw.execute("set enable_seqscan = on")
            tr = raw.transaction()
            await tr.start()
            t = time.perf_counter()
            res = await raw.execute(sql)
            dt = time.perf_counter() - t
            await tr.rollback()
            print(f"cleanup {name}: {res} in {dt:.2f}s (rolled back); plan with seqscan disabled: "
                  + " | ".join(l.strip() for l in plan_noseq[:3]))

    await raw.close()
    await engine.dispose()


if __name__ == "__main__":
    run(main())
