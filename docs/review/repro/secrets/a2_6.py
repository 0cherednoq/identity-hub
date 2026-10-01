import asyncio, sys, time, logging
from datetime import timedelta
from common import *
from identity_hub.testing.memory import MemoryTx, MemoryStore
from identity_hub.service import ProxyPolicy
from identity_hub.errors import LeaseUnavailable, IdentityHubError
from identity_hub.events import EventDraft

logging.getLogger("identity_hub").setLevel(logging.CRITICAL)


async def live_lease(space, identity_id):
    async with space._tx(own=True) as tx:
        return await tx.current_lease(identity_id)


async def a2():
    hub = await mem_hub(); space = hub.space("s1", "S1"); iid = await make_identity(space)
    pool = space.pools.define("p", service="test_service", holder="w")
    orig = MemoryTx.release_lease
    entered = asyncio.Event()

    async def slow(self, *a, **k):
        entered.set()
        await asyncio.sleep(0.3)  # round-trip
        return await orig(self, *a, **k)

    started = asyncio.Event()

    async def worker():
        async with pool.lease() as lease:
            started.set()
            await asyncio.sleep(30)

    t = asyncio.create_task(worker()); await started.wait(); t.cancel()
    await asyncio.gather(t, return_exceptions=True)
    print("A2 control (1 cancel): live lease after exit =", await live_lease(space, iid))
    MemoryTx.release_lease = slow
    try:
        started.clear()
        t = asyncio.create_task(worker()); await started.wait()
        t.cancel(); await entered.wait(); t.cancel()
        res = await asyncio.gather(t, return_exceptions=True)
    finally:
        MemoryTx.release_lease = orig
    row = await live_lease(space, iid)
    print("A2 double cancel: task result =", repr(res[0]), "| live lease =",
          None if row is None else f"lease_id={row.lease_id} expires_at={row.expires_at} now={hub.clock.now()}")
    try:
        async with pool.lease():
            pass
        print("A2: reacquire OK")
    except LeaseUnavailable as e:
        print("A2: reacquire ->", type(e).__name__, e)


async def a3():
    hub = await mem_hub(); space = hub.space("s1", "S1"); iid = await make_identity(space)
    pool = space.pools.define("p", service="test_service", holder="w")
    lease = await pool.acquire()
    try:
        await lease.release(reason="worker-shutdown")
        print("A3: no exception")
    except Exception as e:
        print("A3 release(worker-shutdown) ->", type(e).__name__, e)
    row = await live_lease(space, iid)
    print("A3 _released =", lease._released, "| live lease in store =", row is not None)
    await lease.release()
    print("A3 after second release(): live lease in store =", (await live_lease(space, iid)) is not None)
    try:
        async with pool.lease() as l2:
            pass
        print("A3 next lease OK")
    except LeaseUnavailable as e:
        print("A3 next lease ->", type(e).__name__, e)
    # documented enum values as str work?
    hub = await mem_hub(); space = hub.space("s1", "S1"); iid = await make_identity(space)
    pool = space.pools.define("p", service="test_service", holder="w")
    lease = await pool.acquire()
    await lease.release(reason="failed")
    print("A3 control release(failed): live lease =", (await live_lease(space, iid)) is not None)


async def a4():
    svc = make_service(proxy_policy=ProxyPolicy(group="empty-group"))
    hub = await mem_hub([svc]); space = hub.space("s1", "S1"); iid = await make_identity(space)
    pool = space.pools.define("p", service="test_service", holder="w")
    t0 = time.monotonic()
    try:
        async with pool.lease(wait=3):
            pass
    except LeaseUnavailable:
        pass
    n = sum(1 for ev, _, _ in hub.store._state.events if ev.type == "proxy.pool.exhausted")
    print(f"A4 wait=3 ({time.monotonic()-t0:.2f}s): proxy.pool.exhausted events = {n}")
    t0 = time.monotonic()
    try:
        async with pool.lease(wait=0):
            pass
    except LeaseUnavailable:
        pass
    n2 = sum(1 for ev, _, _ in hub.store._state.events if ev.type == "proxy.pool.exhausted") - n
    print(f"A4 wait=0: +{n2} events")
    t0 = time.monotonic()
    try:
        async with pool.lease(wait=20):
            pass
    except LeaseUnavailable:
        pass
    n3 = sum(1 for ev, _, _ in hub.store._state.events if ev.type == "proxy.pool.exhausted") - n - n2
    print(f"A4 wait=20 ({time.monotonic()-t0:.2f}s): +{n3} events")


async def a5():
    hub = await mem_hub(); space = hub.space("s1", "S1"); iid = await make_identity(space)
    pool = space.pools.define("p", service="test_service", holder="w")
    orig = MemoryTx.decay_error_score

    async def boom(self, *a, **k):
        raise ConnectionError("db down after platform call")

    platform_calls = []
    async with pool.lease() as lease:
        MemoryTx.decay_error_score = boom
        try:
            async with lease.operation("work", key="job-1"):
                platform_calls.append("sent")
            print("A5: no exception")
        except Exception as e:
            print("A5 exception out of lease.operation after successful body ->", type(e).__name__, e,
                  "| platform_calls =", platform_calls)
        finally:
            MemoryTx.decay_error_score = orig
        print("A5 counters:", lease._counters)
        print("A5 operation key state:", hub.store._state.operation_keys.get("job-1"))
        async with lease.operation("work", key="job-1") as op:
            print("A5 retry same key: duplicate =", op.duplicate)


async def a6():
    hub = await mem_hub(); space = hub.space("s1", "S1")
    seen = []

    async def cb(event):
        seen.append(event.data.get("n"))
        if event.data.get("n") == 3:
            raise RuntimeError("poison")

    hub.events.subscribe("x", cb, types={"identity.updated"})
    for n in range(1, 6):
        async with space._tx(own=True) as tx:
            await space._emit(tx, EventDraft("identity.updated", actor="t", data={"n": n}))
    for i in range(5):
        await hub.dispatcher.run_once()
    print("A6 callback invocations over 5 run_once:", seen)
    print("A6 cursor:", hub.store._state.cursors.get("callbacks:x"), "| retrying:",
          hub.dispatcher.runtime["callbacks:x"].retrying)


async def a8_mem():
    svc_a = make_service("svc_a"); svc_b = make_service("svc_b")
    hub = await mem_hub([svc_a, svc_b]); space = hub.space("s1", "S1")
    ia = await make_identity(space, service="svc_a", credentials={"password": "A-SECRET"})
    ib = await make_identity(space, service="svc_b")
    pa = space.pools.define("pa", service="svc_a", holder="wa")
    pb = space.pools.define("pb", service="svc_b", holder="wb")
    la = await pa.acquire(idempotency_key="job-42")
    lb = await pb.acquire(idempotency_key="job-42")
    print(f"A8 mem: A identity={ia}; pool B(svc_b), same key -> identity_id={lb.identity_id}; is A identity={lb.identity_id == ia}; "
          f"same lease_id={lb.lease_id == la.lease_id}; lb.service={lb.service}; identity.service={lb.identity.service}; token shared={lb._token == la._token}")
    try:
        c = await lb.credentials(); print("A8 mem: lb.credentials() ->", c)
    except Exception as e:
        print("A8 mem: lb.credentials() ->", type(e).__name__, e)
    await lb.release()
    print("A8 mem: after B.release(): A lease live =", (await live_lease(space, ia)) is not None)
    s2 = hub.space("s2", "S2"); i2 = await make_identity(s2, service="svc_a")
    la = await pa.acquire(idempotency_key="job-43")
    p2 = s2.pools.define("pa", service="svc_a", holder="w2")
    try:
        l2 = await p2.acquire(idempotency_key="job-43")
        print("A8 mem other space: got own identity =", l2.identity_id == i2, "(memory store has no global unique index)")
    except Exception as e:
        print("A8 mem other space ->", type(e).__name__, e)


async def a8_pg():
    svc_a = make_service("svc_a"); svc_b = make_service("svc_b")
    hub, eng = await pg_hub([svc_a, svc_b]); space = hub.space("s1", "S1")
    ia = await make_identity(space, service="svc_a"); ib = await make_identity(space, service="svc_b")
    pa = space.pools.define("pa", service="svc_a", holder="wa")
    pb = space.pools.define("pb", service="svc_b", holder="wb")
    la = await pa.acquire(idempotency_key="job-42")
    lb = await pb.acquire(idempotency_key="job-42")
    print(f"A8 PG same space: pool B/svc_b got identity {lb.identity_id}; is A identity = {lb.identity_id == ia}; same lease = {lb.lease_id == la.lease_id}")
    s2 = hub.space("s2", "S2"); i2 = await make_identity(s2, service="svc_a")
    p2 = s2.pools.define("pa", service="svc_a", holder="w2")
    try:
        l2 = await p2.acquire(idempotency_key="job-42")
        print("A8 PG other space: got", l2.identity_id)
    except Exception as e:
        print("A8 PG other space ->", type(e).__module__ + "." + type(e).__name__, "|", str(e).splitlines()[0][:300])
        print("A8 PG: is IdentityHubError =", isinstance(e, IdentityHubError))
        print("A8 PG: key value present in message =", "job-42" in str(e))
    await eng.dispose()


async def main():
    for f in (a2, a3, a4, a5, a6, a8_mem):
        try:
            await f()
        except Exception:
            import traceback; traceback.print_exc()
    if "--pg" in sys.argv:
        await a8_pg()


asyncio.run(main())
