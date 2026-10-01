"""A1: один сбой heartbeat при пропорциях по умолчанию (TTL=3h, hb=h, margin=h) -> потеря аренды?"""
import asyncio, sys, time, logging
from datetime import timedelta
from common import *
from identity_hub.errors import LeaseLost

async def scenario(name, hub, patch_cls, latency, fail_calls, scale=1.0, body=None):
    space = hub.space("s1", "S1")
    await make_identity(space)
    pool = space.pools.define("p", service="test_service", holder="w",
                              ttl=timedelta(seconds=3*scale), heartbeat=timedelta(seconds=1*scale),
                              safety_margin=timedelta(seconds=1*scale))
    orig = patch_cls.extend_lease
    calls = []
    t0 = time.monotonic()
    async def patched(self, *a, **k):
        n = len(calls) + 1
        calls.append(round(time.monotonic() - t0, 3))
        if latency:
            await asyncio.sleep(latency)
        if n in fail_calls:
            raise ConnectionError("simulated transient DB failure")
        return await orig(self, *a, **k)
    patch_cls.extend_lease = patched
    result = "survived"
    try:
        async with pool.lease() as lease:
            try:
                await asyncio.sleep(4.5 * scale)
            finally:
                pass
    except LeaseLost as exc:
        result = f"LeaseLost({exc.reason}) at t={time.monotonic()-t0:.3f}s"
    finally:
        patch_cls.extend_lease = orig
    print(f"{name}: extend calls at {calls} -> {result}")

async def main():
    logging.getLogger("identity_hub").setLevel(logging.CRITICAL)
    from identity_hub.testing.memory import MemoryTx
    print("timings: ttl=3s heartbeat=1s safety_margin=1s (пропорция по умолчанию 60/20/20); deadline=last_ok+2s")
    await scenario("mem  no-failure, latency 5ms      ", await mem_hub(), MemoryTx, 0.005, set())
    await scenario("mem  1 failure,  latency 5ms      ", await mem_hub(), MemoryTx, 0.005, {1})
    await scenario("mem  1 failure,  latency 0 (no IO)", await mem_hub(), MemoryTx, 0.0, {1})
    if "--pg" in sys.argv:
        from identity_hub.postgres.store import PgTx
        hub, eng = await pg_hub()
        await scenario("PG   no-failure                   ", hub, PgTx, 0.0, set())
        hub, eng2 = await pg_hub()
        await scenario("PG   1 failure (real PG latency)  ", hub, PgTx, 0.0, {1})
        await eng.dispose(); await eng2.dispose()

asyncio.run(main())
