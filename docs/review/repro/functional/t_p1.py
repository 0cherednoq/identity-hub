from common import *
async def p1_outage():
    svc = make_service("svc")
    hub, clock = mkhub([svc]); await hub.start(); sp = hub.space("s")
    ids = [(await sp.proxies.ensure(f"http://u:p@10.0.0.{i}:80", group="g"))[0].id for i in range(3)]
    for pid in ids:
        for _ in range(3): await sp.proxies.report_failure(pid)
    async def st(): return [str((await sp.proxies.get(p)).status) for p in ids]
    print("  after 3 failures:", await st())
    async def down(url): return False
    async def up(url): return True
    t0 = clock.now(); checks = 0
    while set(await st()) != {"dead"}:
        clock.advance(60); r = await sp.proxies.recheck_due(down); checks += len(r)
    print(f"  provider outage: all dead after {(clock.now()-t0)} ({checks} checks)")
    clock.advance(timedelta(days=1))
    print("  provider is back, recheck_due after 1 day ->", await sp.proxies.recheck_due(up), await st())
    ev = [e for e,_,_ in hub.store._state.events if e.type.startswith("proxy")]
    print("  proxy events:", [(e.type, e.data.get("to")) for e in ev][:8])
asyncio.run(section("P1", p1_outage()))
