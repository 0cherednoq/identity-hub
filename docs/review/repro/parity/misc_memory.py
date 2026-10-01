"""Проверки только MemoryStore/FakeClock/фабрик (без PostgreSQL)."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from datetime import timedelta
from typing import Any

from tests.fixtures.services import FAKE_TG

from identity_hub import IdentityCreate, IdentityHub, Lifecycle, ProbeResult, StaticSecretsProvider
from identity_hub.clock import FakeClock
from identity_hub.dispatch.webhooks import make_signature, verify_signature
from identity_hub.testing import MemoryStore, make_identity, make_service

KEYS = {"k1": b"\x11" * 32}


def section(title: str) -> None:
    print(f"\n=== {title}")


def new_hub(store, clock, services):
    return IdentityHub(secrets=StaticSecretsProvider(KEYS, "k1"), services=services, store=store, clock=clock)


# ---------------------------------------------------------------- 1. один MemoryStore в двух event loop
def loops() -> None:
    section("один MemoryStore в двух asyncio.run()")
    clock = FakeClock()
    store = MemoryStore(clock)
    svc = make_service()

    async def first(contended: bool) -> None:
        hub = new_hub(store, clock, [svc])
        await hub.start()
        space = hub.space("t")
        if contended:
            await asyncio.gather(*(make_identity(space, service=svc) for _ in range(5)))
        else:
            await make_identity(space, service=svc)

    async def second() -> str:
        hub = new_hub(store, clock, [svc])
        await hub.start()
        space = hub.space("t")
        await asyncio.gather(*(make_identity(space, service=svc) for _ in range(5)))
        return f"ok, аккаунтов: {(await space.identities.count())}"

    for contended in (False, True):
        store = MemoryStore(clock)
        asyncio.run(first(contended))
        try:
            print(f"  первый loop {'с конкуренцией' if contended else 'без конкуренции'} ->", asyncio.run(second()))
        except Exception as exc:  # noqa: BLE001
            print(f"  первый loop {'с конкуренцией' if contended else 'без конкуренции'} -> {type(exc).__name__}: {exc}")

    # слушатель диспетчера из закрытого loop
    store = MemoryStore(clock)

    async def with_listener() -> None:
        hub = new_hub(store, clock, [svc])
        await hub.start()
        cm = store.listen("ih_events")
        await cm.__aenter__()  # «забытый» слушатель (тест упал до выхода)
        await make_identity(hub.space("t"), service=svc)

    asyncio.run(with_listener())
    try:
        print("  незакрытый listen() из прошлого loop ->", asyncio.run(second()))
    except Exception as exc:  # noqa: BLE001
        print(f"  незакрытый listen() из прошлого loop -> {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------- 2. стоимость deepcopy
async def perf() -> None:
    section("цена транзакции MemoryStore (deepcopy всего состояния)")
    clock = FakeClock()
    svc = make_service()
    hub = new_hub(MemoryStore(clock), clock, [svc])
    await hub.start()
    space = hub.space("t")
    pool = space.pools.define("p", service=svc)
    total = 0
    for target in (50, 200, 500, 1000):
        while total < target:
            await make_identity(space, service=svc)
            total += 1
        started = time.perf_counter()
        for _ in range(5):
            async with pool.lease() as lease:
                async with lease.operation("work"):
                    pass
        print(f"  {target} аккаунтов: lease+operation = {(time.perf_counter() - started) / 5 * 1000:.0f} мс")


# ---------------------------------------------------------------- 3. фабрики
async def factories() -> None:
    section("фабрики")
    clock = FakeClock()
    svc = make_service()
    hub = new_hub(MemoryStore(clock), clock, [svc, FAKE_TG])
    await hub.start()
    space = hub.space("t")
    made = await space.identities.get(await make_identity(space, service=svc))
    plain = await space.identities.create(IdentityCreate(service=svc, external_ref="plain"))
    print("  make_identity:        ", made.lifecycle, made.auth, made.standing)
    print("  IdentityCreate(default):", plain.lifecycle, plain.auth, plain.standing)
    card = await space.secrets.describe(made.id)
    print("  make_identity credentials present:", card.credential.present, "| sessions:", [s.slot for s in card.sessions])
    try:
        await make_identity(space, service="fake_tg")
        print("  make_identity(service=FAKE_TG) без session: ok")
    except Exception as exc:  # noqa: BLE001
        print(f"  make_identity(service=FAKE_TG) без session: {type(exc).__name__}: {str(exc)[:120]}")
    try:
        await make_identity(space)
    except Exception as exc:  # noqa: BLE001
        print(f"  make_identity(space) с дефолтным service='test_service', которого нет в хабе: {type(exc).__name__}")
    print("  make_service: session/credentials/profile schema =", svc.session.__name__, "(extra=allow),",
          "operations =", list(svc.operations), "| quotas:", svc.operations["work"].quotas)
    # extra=allow: опечатка в поле секрета проходит
    typo = await make_identity(space, service=svc, credentials={"pasword": "x"})
    print("  credentials={'pasword': ...} принято TestingModel:", (await space.secrets.describe(typo)).credential.present)


# ---------------------------------------------------------------- 4. sync.refresh_many и retired
class Probes:
    async def auth(self, ctx: Any) -> ProbeResult | None:
        return ProbeResult("authenticated")

    async def standing(self, ctx: Any) -> ProbeResult | None:
        return None

    async def verification(self, ctx: Any, factor: str) -> ProbeResult | None:
        return None


async def reconcile() -> None:
    section("sync.refresh_many и retired (память)")
    clock = FakeClock()
    svc = replace(FAKE_TG, name="tg_probe", probes=Probes())
    hub = new_hub(MemoryStore(clock), clock, [svc])
    await hub.start()
    space = hub.space("t")
    live = await space.identities.create(
        IdentityCreate(service="tg_probe", external_ref="+1", lifecycle=Lifecycle.READY, session={"auth_key": "k"})
    )
    gone = await space.identities.create(
        IdentityCreate(service="tg_probe", external_ref="+2", lifecycle=Lifecycle.READY, session={"auth_key": "k"})
    )
    await space.identities.retire(gone.id, actor="t", reason="t")
    report = await space.sync.refresh_many(service="tg_probe")
    name = {live.id: "live", gone.id: "retired"}
    print("  probed:", [name[i] for i in report.probed], "| skipped_busy:", [name[i] for i in report.skipped_busy],
          "| failed:", [name[i] for i in report.failed])


# ---------------------------------------------------------------- 5. подпись вебхука и FakeClock
def signature() -> None:
    section("подпись вебхука под FakeClock")
    clock = FakeClock()
    header = make_signature(b"{}", "secret", timestamp=int(clock.now().timestamp()))
    print("  verify_signature() без now:", verify_signature(b"{}", header, "secret"))
    print("  verify_signature(now=clock.now()):", verify_signature(b"{}", header, "secret", now=clock.now()))


# ---------------------------------------------------------------- 6. хаб и хранилище с разными часами
async def clock_mismatch() -> None:
    section("MemoryStore() без clock + IdentityHub(clock=FakeClock)")
    clock = FakeClock()
    svc = make_service()
    hub = new_hub(MemoryStore(), clock, [svc])
    await hub.start()
    space = hub.space("t")
    await make_identity(space, service=svc)
    pool = space.pools.define("p", service=svc)
    async with pool.lease() as lease:
        async with lease.operation("work") as op:
            await op.cool_down(timedelta(minutes=5))
    clock.advance(timedelta(minutes=6))
    try:
        async with pool.lease():
            print("  после advance(6 мин) аккаунт доступен")
    except Exception as exc:  # noqa: BLE001
        print(f"  после advance(6 мин): {type(exc).__name__} — время хранилища не сдвинулось, ошибок конфигурации нет")


# ---------------------------------------------------------------- 7. sync.refresh занятого аккаунта под FakeClock
async def refresh_busy() -> None:
    section("sync.refresh занятого аккаунта под FakeClock (wait=30 внутри библиотеки)")
    clock = FakeClock()
    svc = replace(FAKE_TG, name="tg_probe", probes=Probes())
    hub = new_hub(MemoryStore(clock), clock, [svc])
    await hub.start()
    space = hub.space("t")
    identity = await space.identities.create(
        IdentityCreate(service="tg_probe", external_ref="+1", lifecycle=Lifecycle.READY, session={"auth_key": "k"})
    )
    pool = space.pools.define("p", service="tg_probe")
    async with pool.lease():
        started = time.monotonic()
        try:
            async with asyncio.timeout(5):
                await space.sync.refresh(identity.id)
        except TimeoutError:
            print(f"  HANG: не вернулся за {time.monotonic() - started:.0f} с реального времени (в проде LeaseUnavailable через 30 с)")
        except Exception as exc:  # noqa: BLE001
            print(f"  {type(exc).__name__}")


if __name__ == "__main__":
    import sys

    names = sys.argv[1:]
    table = {"loops": loops, "factories": factories, "reconcile": reconcile, "signature": signature,
             "clock_mismatch": clock_mismatch, "refresh_busy": refresh_busy, "perf": perf}
    for name in names:
        fn = table[name]
        if asyncio.iscoroutinefunction(fn):
            asyncio.run(fn())
        else:
            fn()
