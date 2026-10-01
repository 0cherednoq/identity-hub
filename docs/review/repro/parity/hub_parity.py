"""Сценарии уровня публичного API: хаб на MemoryStore+FakeClock (как pytest-плагин) и на PostgreSQL."""

from __future__ import annotations

import asyncio
import sys
import time
import traceback
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from harness import pg_engine
from pydantic import BaseModel, SecretStr
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker
from tests.fixtures.services import FAKE_EXCHANGE, FAKE_TG, KycEvidence

from identity_hub import (
    AuthState,
    IdentityCreate,
    IdentityHub,
    Lifecycle,
    ProxyPolicy,
    StaticSecretsProvider,
)
from identity_hub.clock import FakeClock
from identity_hub.service import ProxyDownPolicy
from identity_hub.read.events import EventFilters
from identity_hub.testing import MemoryStore

KEYS = {"k1": b"\x11" * 32}
TG_PROXY = replace(
    FAKE_TG,
    name="tg_proxy",
    proxy_policy=ProxyPolicy(group="res", sticky=True, on_proxy_down=ProxyDownPolicy.WAIT),
)


class LevelProfile(BaseModel):
    level: int | None = None
    nick: str | None = None


TG_LEVEL = replace(FAKE_TG, name="tg_level", profile=LevelProfile, filterable_profile=frozenset({"level", "nick"}),
                   searchable_profile=frozenset())
SERVICES = [FAKE_TG, FAKE_EXCHANGE, TG_PROXY, TG_LEVEL]


async def tg(space, ref, service="fake_tg", **kw):
    kw.setdefault("lifecycle", Lifecycle.READY)
    kw.setdefault("auth_state", AuthState.AUTHENTICATED)
    return (await space.identities.create(IdentityCreate(service=service, external_ref=ref, **kw))).id


async def tick(clock, seconds):
    if clock is not None:
        clock.advance(seconds)
    else:
        await asyncio.sleep(seconds)


# ------------------------------------------------------------------ аренда и часы


async def wait_times_out(hub, space, clock):
    pool = space.pools.define("empty", service="fake_tg")
    started = time.monotonic()
    try:
        async with asyncio.timeout(4):
            async with pool.lease(wait=0.3):
                return "leased?!"
    except TimeoutError:
        return f"HANG: wait=0.3 не истёк за {time.monotonic() - started:.1f} с реального времени"
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__} через {time.monotonic() - started:.1f} с"


async def advance_inside_lease(hub, space, clock):
    await tg(space, "+1")
    pool = space.pools.define("p", service="fake_tg")
    try:
        async with pool.lease() as lease:
            if clock is not None:
                clock.advance(timedelta(seconds=45))  # < TTL 60 с
            lost = lease.lost
            async with lease.operation("read_channel"):
                pass
        return f"ok, lease.lost={lost}"
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


async def among_select(hub, space, clock):
    identity_id = await tg(space, "+1")
    ids = hub.read.identities
    pool = space.pools.define("p", service="fake_tg")
    try:
        async with pool.lease(among=select(ids.c.id).where(ids.c.external_ref == "+1")) as lease:
            return lease.identity_id == identity_id
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


async def profile_filter_none(hub, space, clock):
    await tg(space, "+1")
    await tg(space, "+2", profile={"country": "ru"})
    pool = space.pools.define("p", service="fake_tg")
    try:
        async with pool.lease(profile={"country": None}) as lease:
            return (await space.identities.get(lease.identity_id)).external_ref
    except Exception as exc:  # noqa: BLE001
        return type(exc).__name__


async def delete_frees_sticky_proxy(hub, space, clock):
    await space.proxies.create("http://10.9.0.1:80", group="res", max_identities=1)
    first = await tg(space, "+1", service="tg_proxy")
    pool = space.pools.define("p", service="tg_proxy")
    async with pool.lease() as lease:
        used = lease.proxy_id
    await space.identities.delete(first, actor="t")
    await tg(space, "+2", service="tg_proxy")
    try:
        async with pool.lease() as lease:
            return "ok", lease.proxy_id == used
    except Exception as exc:  # noqa: BLE001
        return type(exc).__name__, str(exc)[:80]


async def maintenance_lease_expired_event(hub, space, clock):
    await tg(space, "+1")
    pool = space.pools.define("p", service="fake_tg", ttl=timedelta(seconds=1.2),
                              heartbeat=timedelta(seconds=0.4), safety_margin=timedelta(seconds=0.4))
    await pool.acquire()  # воркер «умер»: нет heartbeat и release
    await tick(clock, 1.5)
    await hub.maintenance.run_all()
    page = await space.admin.event_page(filters=EventFilters(include_journal=True))
    return [sorted(item.data) for item in page.items if item.type == "lease.expired"]


async def event_page_order(hub, space, clock):
    await tg(space, "+1")
    pool = space.pools.define("p", service="fake_tg")
    for _ in range(3):
        async with pool.lease() as lease:
            async with lease.operation("read_channel"):
                pass
    page = await space.admin.event_page(filters=EventFilters(include_journal=True))
    types = [item.type for item in page.items if item.type.startswith("lease.")]
    return types == ["lease.released", "lease.acquired"] * 3 or types


async def lease_stats_by_reason(hub, space, clock):
    identity_id = await tg(space, "+1")
    pool = space.pools.define("p", service="fake_tg")
    async with pool.lease():
        stats = await space.admin.lease_stats(identity_id, datetime(2020, 1, 1, tzinfo=UTC))
    return str(stats)


# ------------------------------------------------------------------ связи, секреты, верификации


async def impact_path(hub, space, clock):
    a = await tg(space, "+1")
    b = await tg(space, "+2", links={"recovery": a})
    await tg(space, "+3", links={"owner": b})
    return [(i.depth, list(i.path)) for i in await space.links.impact(a)]


async def links_of_order(hub, space, clock):
    b, c = await tg(space, "+2"), await tg(space, "+3")
    a = await tg(space, "+1")
    await space.links.add(a, "zz", b)
    await space.links.add(a, "aa", c)
    return [link.role for link in (await space.links.of(a)).outgoing]


async def secrets_describe_versions(hub, space, clock):
    identity_id = await tg(space, "+1", credentials={"phone": "+1"}, session={"auth_key": "k"})
    await space.secrets.replace_credentials(identity_id, {"phone": "+1", "password": "x"}, actor="t")
    card = await space.secrets.describe(identity_id)
    bare = await space.secrets.describe(await tg(space, "+2", auth_state=AuthState.NO_SESSION))
    return {
        "cred": (card.credential.version, card.credential.updated_at is not None),
        "session": [(s.version, s.updated_at is not None) for s in card.sessions],
        "no_credentials": (bare.credential.present, bare.credential.version),
    }


async def kyc_evidence_after_submit(hub, space, clock):
    identity = await space.identities.create(
        IdentityCreate(
            service="fake_exchange", external_ref="t@x", lifecycle=Lifecycle.READY,
            credentials={"email": "t@x", "password": "p"}, session={"cookies": {"sid": "1"}},
        )
    )
    lease = await space.pools.define("ex", service="fake_exchange").acquire(among=[identity.id])
    for factor in ("email", "phone"):
        v = lease.verification(factor)
        await v.start()
        await v.submit()
    kyc = lease.verification("kyc_id")
    await kyc.start(KycEvidence(document_number=SecretStr("AB123456")))
    await kyc.submit()
    try:
        evidence = await kyc.evidence()
        return type(evidence).__name__
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {str(exc)[:90]}"
    finally:
        await lease.release()


# ------------------------------------------------------------------ уведомления, фон


async def notification_rule_errors_and_order(hub, space, clock):
    notifications = space.notifications
    try:
        await notifications.create_rule(name="x", channel_id=uuid4(), event_types=["*"], created_by="t")
        err = "no error"
    except Exception as exc:  # noqa: BLE001
        err = type(exc).__name__
    channel = await notifications.create_telegram_channel(title="ops", bot_token=SecretStr("123456:" + "A" * 35), chat_id="1")
    for name in ("zeta", "alpha"):
        await tick(clock, 0.05)
        await notifications.create_rule(name=name, channel_id=channel.id, event_types=["*"], created_by="t")
    return err, [rule.name for rule in await notifications.rules()]


async def background_snapshot_scoped(hub, space, clock):
    await tg(space, "+1")
    try:
        snapshot = await hub.background_snapshot([space.slug])
        return "ok", type(snapshot).__name__
    except Exception as exc:  # noqa: BLE001
        return type(exc).__name__, str(exc)[:90]


async def search_id_tail(hub, space, clock):
    identity_id = await tg(space, "+1")
    page12 = await space.identities.search(f"id:{identity_id.hex[-12:]}")
    page16 = await space.identities.search(f"id:{identity_id.hex[-16:]}")
    return len(page12.items), len(page16.items)


async def profile_sort_pages(hub, space, clock):
    from identity_hub.read.search import AccountSearchOptions

    out = {}
    for key, values in (("level", [1, 2, 10, 3]), ("nick", ["bob", "Alice", "_x", "Zed"])):
        for n, value in enumerate(values):
            await tg(space, f"+{key}{n}", service="tg_level", profile={key: value})
        options = AccountSearchOptions(sort_by=f"profile:{key}", sort_desc=False)
        try:
            first = await space.admin.identity_page(f"service:tg_level", options=options, limit=2)
            got = [i.profile.get(key) for i in first.items]
            second = await space.admin.identity_page(f"service:tg_level", options=options, limit=2, after=first.next_cursor)
            out[key] = got + [i.profile.get(key) for i in second.items]
        except Exception as exc:  # noqa: BLE001
            out[key] = f"{type(exc).__name__}: {str(exc)[:70]}"
    return out


async def quarantine_with_until(hub, space, clock):
    from identity_hub import Override

    identity_id = await tg(space, "+1")
    async with space._tx(own=True) as tx:
        now = await tx.now()
    await space.identities.override(
        identity_id,
        Override("availability", "", "quarantined", reason="t", actor="t", until=now + timedelta(seconds=1.5)),
    )
    await tick(clock, 2)
    pool = space.pools.define("p", service="fake_tg")
    try:
        async with pool.lease():
            return "leased"
    except Exception as exc:  # noqa: BLE001
        return type(exc).__name__


SCENARIOS = [
    quarantine_with_until,
    profile_sort_pages,
    wait_times_out,
    advance_inside_lease,
    among_select,
    profile_filter_none,
    delete_frees_sticky_proxy,
    maintenance_lease_expired_event,
    event_page_order,
    lease_stats_by_reason,
    impact_path,
    links_of_order,
    secrets_describe_versions,
    kyc_evidence_after_submit,
    notification_rule_errors_and_order,
    background_snapshot_scoped,
    search_id_tail,
]


async def main():
    from identity_hub.postgres import metadata

    engine = await pg_engine()
    tables = ", ".join(t.name for t in metadata.sorted_tables)
    wanted = set(sys.argv[1:])
    for fn in SCENARIOS:
        if wanted - {"-v"} and fn.__name__ not in wanted:
            continue
        out = {}
        for kind in ("memory", "pg"):
            secrets = StaticSecretsProvider(KEYS, "k1")
            clock = None
            if kind == "memory":
                clock = FakeClock()
                hub = IdentityHub(secrets=secrets, services=SERVICES, store=MemoryStore(clock), clock=clock,
                                  strict_events=True)
            else:
                async with engine.begin() as conn:
                    await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
                hub = IdentityHub(async_sessionmaker(engine, expire_on_commit=False), secrets, SERVICES,
                                  strict_events=True)
            await hub.start()
            try:
                out[kind] = await asyncio.wait_for(fn(hub, hub.space("test", "Test"), clock), 40)
            except Exception as exc:  # noqa: BLE001
                out[kind] = f"EXC {type(exc).__name__}: {str(exc)[:200]}"
                if "-v" in wanted:
                    traceback.print_exc()
        print()
        print(f"=== {fn.__name__}: {'SAME' if out['memory'] == out['pg'] else 'DIFF'}")
        print("  memory:", out["memory"])
        print("  pg    :", out["pg"])
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
