"""E6-E16: рантайм-проверки SQL-слоя (отдельная база, db_schema='ihs' — вне search_path)."""

from __future__ import annotations

import asyncio
import sys
import time
import traceback
from datetime import timedelta
from uuid import uuid4

from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from identity_hub._ids import new_id
from identity_hub.api.identities import IdentityCreate
from identity_hub.crypto import StaticSecretsProvider
from identity_hub.hub import IdentityHub
from identity_hub.postgres.migrate import migrate
from identity_hub.read.search import AccountSearchOptions
from identity_hub.service import Service
from identity_hub.store.base import NotificationEnqueue
from sch_common import fresh_db, run

SCHEMA = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] != "public" else None
ONLY = set(sys.argv[2:])


class Profile(BaseModel):
    age: int | None = None
    country: str | None = None


class Session(BaseModel):
    key: str


SVC = Service(
    name="svc",
    title="Svc",
    profile=Profile,
    session=Session,
    filterable_profile=frozenset({"age", "country"}),
)


def section(name: str):  # type: ignore[no-untyped-def]
    def deco(fn):  # type: ignore[no-untyped-def]
        async def wrapper(*a, **kw):  # type: ignore[no-untyped-def]
            if ONLY and name not in ONLY:
                return
            print(f"\n===== {name}")
            try:
                await fn(*a, **kw)
            except Exception as exc:
                first = str(exc).strip().splitlines()[0][:400] if str(exc).strip() else ""
                print(f"[{name}] EXCEPTION {type(exc).__name__}: {first}")
                if "-v" in sys.argv:
                    traceback.print_exc()

        return wrapper

    return deco


def q(name: str) -> str:
    return f'"{SCHEMA}".{name}' if SCHEMA else name


async def bulk_identities(engine, space_id, n, **cols):  # type: ignore[no-untyped-def]
    extra_cols = "".join(f", {k}" for k in cols)
    extra_vals = "".join(f", :{k}" for k in cols)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                f"""
            insert into {q('ih_identities')}(id, space_id, service, external_ref, normalized_ref, lifecycle,
                standing, auth_state, availability, error_score, fencing_seq, event_seq{extra_cols})
            select gen_random_uuid(), :space, 'svc', r, r, 'ready', 'good', 'authenticated', 'available', 0, 0, 0
                   {extra_vals}
            from (select 'bulk-' || gen_random_uuid() as r from generate_series(1, :n)) s
            """
            ),
            {"space": space_id, "n": n, **cols},
        )


@section("E16 db_schema smoke")
async def e16(hub, space, engine):  # type: ignore[no-untyped-def]
    ident = await space.identities.create(
        IdentityCreate(service="svc", external_ref="smoke-1", session={"key": "k"}, profile={"age": 30})
    )
    await space.identities.activate(ident.id, actor="t")
    pool = space.pools.define("p", service=SVC)
    ref = await space.ref()
    ids = hub.read.identities
    among = select(ids.c.id).where(ids.c.space_id == ref.id, ids.c.standing == "good")
    async with pool.lease(among=among) as lease:
        print("lease via among=Select ok:", lease.identity_id == ident.id)
    print("search:", len((await space.identities.search("smoke")).items))
    print("dispatcher.run_once:", await hub.dispatcher.run_once())
    print("maintenance.run_all:", dict((await hub.maintenance.run_all()).counts))
    print("doctor:", await hub.doctor.check())
    print("snapshot:", await hub.state_snapshots.capture())
    print("metrics_snapshot ok:", bool(await hub.store.metrics_snapshot()))
    async with hub.store.listen("identity_hub_events") as signal:
        print("listen signal:", signal is not None)
    # read-схема из сессии приложения без карты трансляции
    async with async_sessionmaker(engine)() as s:
        print("read schema rows:", (await s.execute(select(ids.c.id, ids.c.auth))).all()[:1])


@section("E6 expire_leases starvation")
async def e6(hub, space, engine):  # type: ignore[no-untyped-def]
    ref = await space.ref()
    pool = space.pools.define("p6", service=SVC)
    one = await space.identities.create(IdentityCreate(service="svc", external_ref="e6-one", session={"key": "k"}))
    await space.identities.activate(one.id, actor="t")
    async with pool.lease(among=[one.id]):
        pass
    async with engine.connect() as conn:
        print(
            "after normal release ih_leases row stays:",
            (await conn.execute(text(f"select count(*), bool_and(expires_at <= now()) from {q('ih_leases')}"))).all(),
        )
    # 600 «нормально освобождённых» аренд (строка остаётся с expires_at в прошлом, история закрыта)
    await bulk_identities(engine, ref.id, 601, note="e6")
    async with engine.begin() as conn:
        await conn.execute(
            text(
                f"""
            insert into {q('ih_leases')}(identity_id, space_id, lease_id, token, fencing, pool, holder, expires_at)
            select id, space_id, gen_random_uuid(), gen_random_uuid(), 1, 'p6', 'h', now() - interval '2 hours'
            from {q('ih_identities')} where note = 'e6'
            """
            )
        )
        await conn.execute(
            text(
                f"""
            insert into {q('ih_lease_history')}(lease_id, space_id, identity_id, token, fencing, pool, holder,
                                                 succeeded, failed, released_at, end_reason)
            select lease_id, space_id, identity_id, token, 1, 'p6', 'h', 0, 0, now() - interval '2 hours', 'released'
            from {q('ih_leases')} where pool = 'p6' and holder = 'h'
            """
            )
        )
        # одна «упавшая» аренда: истекла минуту назад, история открыта
        await conn.execute(
            text(
                f"""
            update {q('ih_leases')} set expires_at = now() - interval '1 minute', holder = 'crashed'
            where identity_id = (select identity_id from {q('ih_leases')} where holder = 'h' limit 1)
            """
            )
        )
        await conn.execute(
            text(
                f"""
            update {q('ih_lease_history')} set released_at = null, end_reason = null
            where lease_id = (select lease_id from {q('ih_leases')} where holder = 'crashed')
            """
            )
        )
    for i in range(3):
        report = await hub.maintenance._leases()
        print(f"maintenance leases tick {i}:", dict(report.counts))
    async with engine.connect() as conn:
        print(
            "crashed lease history still open:",
            (
                await conn.execute(
                    text(f"select count(*) from {q('ih_lease_history')} where released_at is null and holder='h'")
                )
            ).scalar(),
        )


@section("E7 lost update tags")
async def e7(hub, space, engine):  # type: ignore[no-untyped-def]
    ident = await space.identities.create(IdentityCreate(service="svc", external_ref="e7"))
    await asyncio.gather(*(space.identities.add_tags(ident.id, [f"t{i}"]) for i in range(20)))
    got = await space.identities.get(ident.id)
    print("20 concurrent add_tags → tags kept:", len(got.tags))
    await asyncio.gather(
        *(space.identities.update(ident.id, labels={f"k{i}": i}) for i in range(20))
    )
    got = await space.identities.get(ident.id)
    print("20 concurrent label merges → labels kept:", len(got.labels))


@section("E8 delete identity with live lease")
async def e8(hub, space, engine):  # type: ignore[no-untyped-def]
    ident = await space.identities.create(IdentityCreate(service="svc", external_ref="e8", session={"key": "k"}))
    await space.identities.activate(ident.id, actor="t")
    pool = space.pools.define("p8", service=SVC)
    try:
        async with pool.lease(among=[ident.id]) as lease:
            await space.identities.delete(ident.id, actor="admin")
            print("delete during live lease: allowed")
            lease_id = lease.lease_id
    except Exception as exc:
        print("lease exit raised:", type(exc).__name__, str(exc)[:200])
    await hub.maintenance.run_all()
    async with engine.connect() as conn:
        print(
            "history row of deleted identity (released_at, end_reason):",
            (
                await conn.execute(
                    text(f"select released_at, end_reason from {q('ih_lease_history')} where identity_id = :i"),
                    {"i": ident.id},
                )
            ).all(),
        )


@section("E10 profile sort cursor")
async def e10(hub, space, engine):  # type: ignore[no-untyped-def]
    for i in range(3):
        await space.identities.create(
            IdentityCreate(service="svc", external_ref=f"e10-{i}", profile={"age": 20 + i, "country": "UA"})
        )
    opts = AccountSearchOptions(sort_by="profile:age")
    page = await space._account_search("e10", options=opts, limit=1)
    print("page1 ok, cursor:", page.next_cursor is not None)
    page2 = await space._account_search("e10", options=opts, limit=1, after=page.next_cursor)
    print("page2 ok:", [i.external_ref for i in page2.items])


@section("E10b created_at cursor with mismatched kind")
async def e10b(hub, space, engine):  # type: ignore[no-untyped-def]
    import base64
    import json

    raw = json.dumps([1, "created_at", True, "string", "zzz", str(uuid4())]).encode()
    cur = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    await space._account_search("", options=AccountSearchOptions(), limit=1, after=cur)
    print("accepted")


@section("E11 IN-list > 32767")
async def e11(hub, space, engine):  # type: ignore[no-untyped-def]
    ref = await space.ref()
    await bulk_identities(engine, ref.id, 33000, note="e11")
    async with engine.begin() as conn:
        await conn.execute(
            text(f"update {q('ih_identities')} set availability='quarantined' where note='e11'")
        )
    t = time.perf_counter()
    rows = await space.admin.human_queue()
    print("human_queue ok", time.perf_counter() - t, type(rows).__name__)


@section("E12 doctor scaling")
async def e12(hub, space, engine):  # type: ignore[no-untyped-def]
    ref = await space.ref()
    async with engine.begin() as conn:
        await conn.execute(text(f"delete from {q('ih_identities')} where note in ('e11','e6')"))
    for n in (2000, 4000, 8000):
        await bulk_identities(engine, ref.id, n, note=f"e12-{n}")
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    f"""
                insert into {q('ih_region_states')}(identity_id, region, key, space_id, state, change_source, version)
                select id, r.region, r.key, space_id, r.state, 'command', 1
                from {q('ih_identities')} cross join (values ('standing','','good'), ('auth','default','authenticated'),
                     ('verification','email','verified')) r(region, key, state)
                where note = :note
                """
                ),
                {"note": f"e12-{n}"},
            )
            total = (await conn.execute(text(f"select count(*) from {q('ih_identities')}"))).scalar()
        t = time.perf_counter()
        issues = await hub.doctor.check()
        print(f"identities={total}: doctor.check {time.perf_counter() - t:.1f}s, issues={len(issues)}")


@section("E14 duplicate non-digest notification")
async def e14(hub, space, engine):  # type: ignore[no-untyped-def]
    ref = await space.ref()
    channel, rule = new_id(), new_id()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                f"""insert into {q('ih_notification_channels')}(id, space_id, kind, title, config, enabled, key_id,
                    wrapped_dek, nonce, ciphertext) values (:c, :s, 'telegram', 't', '{{}}', true, 'k', '', '', '')"""
            ),
            {"c": channel, "s": ref.id},
        )
        await conn.execute(
            text(
                f"""insert into {q('ih_notification_rules')}(id, space_id, channel_id, name, enabled, event_types,
                    min_severity, created_by) values (:r, :s, :c, 'r', true, '{{*}}', 'info', 'x')"""
            ),
            {"r": rule, "c": channel, "s": ref.id},
        )
    event_id = new_id()
    now = hub.clock.now()
    item = NotificationEnqueue(rule, channel, event_id, None, now)
    await hub.store.enqueue_notifications([item])
    await hub.store.enqueue_notifications([item])  # повтор пачки после сбоя между enqueue и сдвигом курсора
    async with engine.connect() as conn:
        print(
            "rows for one (rule, event):",
            (
                await conn.execute(
                    text(f"select count(*) from {q('ih_notifications')} where :e = any(event_ids)"), {"e": event_id}
                )
            ).scalar(),
        )


async def main() -> None:
    engine = await fresh_db("sch_e6" + ("_" + SCHEMA if SCHEMA else ""))
    await migrate(engine, db_schema=SCHEMA)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    hub = IdentityHub(sessions, StaticSecretsProvider({"k1": b"\x11" * 32}, "k1"), [SVC], db_schema=SCHEMA)
    await hub.start()
    space = hub.space("rev", "Review")
    for fn in (e16, e6, e7, e8, e10, e10b, e11, e12, e14):
        await fn(hub, space, engine)
    await engine.dispose()


if __name__ == "__main__":
    run(main())
