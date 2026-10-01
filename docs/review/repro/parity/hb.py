import asyncio, time
from datetime import timedelta
from identity_hub import IdentityHub, StaticSecretsProvider
from identity_hub.clock import FakeClock
from identity_hub.testing import MemoryStore, make_identity, make_service

async def main():
    clock = FakeClock(); svc = make_service()
    hub = IdentityHub(secrets=StaticSecretsProvider({"k": b"\x01"*32}, "k"), services=[svc], store=MemoryStore(clock), clock=clock)
    await hub.start(); space = hub.space("t")
    ident = await make_identity(space, service=svc)
    pool = space.pools.define("p", service=svc)
    lost = []
    # 1) отзыв аренды: воркер узнаёт только на реальном heartbeat (20 с по умолчанию)
    try:
        async with pool.lease(on_lost=lambda: lost.append(time.monotonic())) as lease:
            await space.pools.revoke(ident, reason="t", actor="t")
            clock.advance(timedelta(seconds=25))       # «прошло» больше одного heartbeat
            await asyncio.sleep(1.0)
            print("revoke + advance(25s) + 1 c реального времени: lease.lost =", lease.lost, "| on_lost вызван:", bool(lost))
            try:
                async with lease.operation("work"):
                    print("  операция под отозванной арендой выполнена")
            except Exception as exc:
                print("  операция:", type(exc).__name__, exc)
    except Exception as exc:
        print("выход из блока:", type(exc).__name__)
    # 2) короткие тайминги: heartbeat идёт по реальному времени, TTL в хранилище — по FakeClock
    fast = space.pools.define("fast", service=svc, ttl=timedelta(seconds=0.6), heartbeat=timedelta(seconds=0.2), safety_margin=timedelta(seconds=0.2))
    async with fast.lease() as lease:
        before = lease.expires_at
        await asyncio.sleep(0.7)   # 3 реальных heartbeat, FakeClock стоит
        print("ttl=0.6s, прошло 0.7 c реального времени: lost =", lease.lost, "| expires_at сдвинулся:", lease.expires_at != before)
asyncio.run(main())
