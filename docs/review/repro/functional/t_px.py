from common import *
from identity_hub import VerificationSpec, VerificationMode, ProxyPolicy
from identity_hub.importing import ImportRecord
from identity_hub.proxies import parse_proxy_url

async def v1_review():
    svc = make_service("svc", verifications=(VerificationSpec("kyc", mode=VerificationMode.MANUAL, max_attempts=1, valid_for=timedelta(days=1)),),
                       operations={"work": OperationRule(verified=frozenset({"kyc"}))})
    hub, clock = mkhub([svc]); await hub.start(); sp = hub.space("s")
    iid = await make_identity(sp, service=svc)
    pool = sp.pools.define("p", service=svc, holder="w")
    lease = await pool.acquire(for_operation="probe", among=[iid])
    await lease.verification("kyc").start()
    await lease.release()
    st = await sp.verifications.review(iid, "kyc", "approve", actor="op", reason="looks fine")
    print("  approve straight from pending (MANUAL, no submit):", st.state, "attempts:", st.attempts)
    clock.advance(timedelta(days=3))
    try:
        l2 = await pool.acquire(for_operation="work", among=[iid])
        print("  3 days later (valid_for=1d), no maintenance run: lease for op requiring kyc granted; verified_factors =", set((await sp.identities.get(iid)).verified_factors))
        try:
            async with l2.operation("work"): pass
            print("  operation 'work' allowed with expired verification")
        except Exception as e: print("  op ->", type(e).__name__, e)
        await l2.release()
    except Exception as e: print("  ->", type(e).__name__, e)

async def p1_outage():
    svc = make_service("svc")
    hub, clock = mkhub([svc]); await hub.start(); sp = hub.space("s")
    ids = [(await sp.proxies.ensure(f"http://u:p@10.0.0.{i}:80", group="g"))[0].id for i in range(3)]
    for pid in ids:
        for _ in range(3): await sp.proxies.report_failure(pid)
    print("  after 3 failures:", [str((await sp.proxies.get(p)).status) for p in ids])
    async def down(url): return False
    async def up(url): return True
    t0 = clock.now(); checks = 0
    while any((await sp.proxies.get(p)).status != "dead" for p in ids):
        clock.advance(60); r = await sp.proxies.recheck_due(down); checks += len(r)
    print(f"  provider outage: all dead after {(clock.now()-t0)} ({checks} checks)")
    clock.advance(timedelta(days=1))
    print("  provider is back, recheck_due after 1 day ->", await sp.proxies.recheck_due(up), [str((await sp.proxies.get(p)).status) for p in ids])

async def p2_pin_bypass():
    svc = make_service("svc")
    hub, clock = mkhub([svc]); await hub.start(); sp = hub.space("s")
    pid = (await sp.proxies.ensure("http://u:p@10.0.0.1:80", group="g"))[0].id
    await sp.proxies.override(pid, "quarantined", reason="hold", actor="op", pin_for=timedelta(days=7))
    async def down(url): return False
    for _ in range(8):
        clock.advance(3600); await sp.proxies.recheck_due(down)
    p = await sp.proxies.get(pid)
    print("  pinned(7d) quarantined proxy after failed rechecks:", p.status, "pinned_until:", p.pinned_until)

async def p3_import_capacity():
    svc = make_service("svc", proxy_policy=ProxyPolicy(group="g"))
    hub, clock = mkhub([svc]); await hub.start(); sp = hub.space("s")
    rep = await sp.imports.run(service=svc, records=[ImportRecord(f"r{i}", proxy="http://u:p@10.0.0.1:80") for i in range(10)])
    page = await sp.proxies.list()
    print("  created:", len(rep.created), "proxy max_identities:", page.items[0].max_identities, "bound_count:", page.items[0].bound_count)
    print("  repr(parse_proxy_url):", repr(parse_proxy_url("http://user:S3cret@10.0.0.1:80")))
    print("  Proxies API:", [m for m in dir(sp.proxies) if not m.startswith("_")])

async def main():
    await section("V1 review/expiry", v1_review())
    await section("P1 outage -> dead forever", p1_outage())
    await section("P2 pin bypass", p2_pin_bypass())
    await section("P3 import capacity", p3_import_capacity())
asyncio.run(main())
