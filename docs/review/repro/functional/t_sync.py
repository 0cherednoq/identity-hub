from common import *
from identity_hub import Lifecycle, Blame, Fault
from identity_hub.maintenance import MaintenanceSchedule

async def s1_starvation():
    calls = []
    class P:
        async def auth(self, ctx): calls.append(ctx.identity_id); return ProbeResult("authenticated")
        async def standing(self, ctx): return ProbeResult("good")
        async def verification(self, ctx, f): return None
    svc = make_service("svc", probes=P())
    hub, clock = mkhub([svc]); await hub.start(); sp = hub.space("s")
    prov = [await make_identity(sp, service=svc, lifecycle=Lifecycle.PROVISIONING) for _ in range(3)]
    ready = [await make_identity(sp, service=svc) for _ in range(2)]
    for i in range(3):
        rep = await sp.sync.refresh_many(limit=3)
        print(f"  run {i}: probed={len(rep.probed)} skipped={len(rep.skipped_busy)} failed={[v[:60] for v in rep.failed.values()]}")
    print("  ready identities ever probed:", any(r in calls for r in ready))

async def s2_probe_raises():
    class P:
        async def auth(self, ctx): raise RuntimeError("platform 502")
    class C:
        def classify(self, exc): return Fault(Blame.IDENTITY)
    for clf in (None, C()):
        svc = make_service("svc", probes=P(), error_classifier=clf)
        hub, clock = mkhub([svc]); await hub.start(); sp = hub.space("s")
        iid = await make_identity(sp, service=svc)
        for i in range(6):
            try: await sp.sync.refresh(iid, ["auth"])
            except Exception as e: last = f"{type(e).__name__}: {e}"[:100]
        ident = await sp.identities.get(iid)
        print(f"  classifier={'IDENTITY' if clf else None}: last={last!r} error_score={ident.error_score} availability={ident.availability}")

async def s3_only_one_method_and_misc():
    class P:
        async def standing(self, ctx): return ProbeResult("good", observed_at=datetime(2030,1,1))  # naive
    svc = make_service("svc", probes=P())
    hub, clock = mkhub([svc]); await hub.start(); sp = hub.space("s")
    iid = await make_identity(sp, service=svc)
    try: print("  auth only-standing Probes:", await sp.sync.refresh(iid, ["auth"]))
    except Exception as e: print("  ->", type(e).__name__, e)
    try: print("  naive observed_at:", await sp.sync.refresh(iid, ["standing"]))
    except Exception as e: print("  naive observed_at ->", type(e).__name__, e)
    try: print("  refresh_many(verification) no factors:", await sp.sync.refresh_many(["verification"]))
    except Exception as e: print("  refresh_many(['verification']) ->", type(e).__name__, e)

async def s4_slow_probe():
    h = {}
    class P:
        async def standing(self, ctx):
            h["c"].advance(45); return ProbeResult("banned")
    svc = make_service("svc", probes=P())
    hub, clock = mkhub([svc]); h["c"] = clock; await hub.start(); sp = hub.space("s")
    iid = await make_identity(sp, service=svc)
    try: print("  result:", await sp.sync.refresh(iid, ["standing"]))
    except Exception as e: print("  ->", type(e).__name__, str(e)[:120])
    print("  standing:", (await sp.identities.get(iid)).standing)

async def s5_timer_starvation():
    svc = make_service("svc")
    hub, clock = mkhub([svc], maintenance=MaintenanceSchedule(batch_size=2)); await hub.start(); sp = hub.space("s")
    now = clock.now()
    frozen = [await make_identity(sp, service=svc) for _ in range(2)]
    for f in frozen:
        await sp.identities.override(f, Override("standing", "", "frozen", reason="tmp freeze", actor="op", until=now + timedelta(minutes=1)))
    lim = await make_identity(sp, service=svc)
    await sp.identities.override(lim, Override("standing", "", "limited", reason="x", actor="op", until=now + timedelta(minutes=5)))
    clock.advance(timedelta(hours=2))
    for i in range(3):
        rep = await hub.maintenance.run_all()
        print(f"  maintenance {i}: region_timers={rep.counts.get('region_timers')}", "limited identity standing:", (await sp.identities.get(lim)).standing)

async def s6_override_verification_and_key():
    from identity_hub import VerificationSpec
    svc = make_service("svc", verifications=(VerificationSpec("kyc"),), operations={"work": OperationRule(verified=frozenset({"kyc"}))})
    hub, clock = mkhub([svc]); await hub.start(); sp = hub.space("s")
    iid = await make_identity(sp, service=svc)
    await sp.identities.override(iid, Override("verification", "kyc", "verified", reason="manual", actor="op"))
    st = await sp.verifications.status(iid, "kyc")
    ident = await sp.identities.get(iid)
    print("  region state:", st.state, "identity.verified_factors:", set(ident.verified_factors))
    card = await sp.identities.card(iid)
    print("  eligible:", card.eligible, card.blockers)
    await sp.identities.override(iid, Override("standing", "typo", "banned", reason="manual", actor="op"))
    ident = await sp.identities.get(iid)
    print("  after Override(standing,key='typo',banned): identity.standing =", ident.standing)
    try:
        await sp.identities.restore_standing(iid, actor="op", reason="r")
        print("  restore ok ->", (await sp.identities.get(iid)).standing)
    except Exception as e: print("  restore ->", type(e).__name__, e)
    await sp.identities.override(iid, Override("verification", "nonexistent_factor", "verified", reason="manual", actor="op"))
    print("  override of undeclared factor accepted")

async def s7_removed_service_poisons_maintenance():
    a = make_service("a"); b = make_service("b")
    hub, clock = mkhub([a, b]); await hub.start(); sp = hub.space("s")
    ia = await make_identity(sp, service=a); ib = await make_identity(sp, service=b)
    now = clock.now()
    await sp.identities.override(ib, Override("standing", "", "limited", reason="x", actor="op", until=now + timedelta(minutes=1)))
    await sp.identities.override(ia, Override("standing", "", "limited", reason="x", actor="op", until=now + timedelta(minutes=5)))
    clock.advance(timedelta(hours=1))
    hub2 = IdentityHub(store=hub.store, secrets=StaticSecretsProvider({"k": bytes(range(32))}, active="k"), services=[a], clock=clock)
    await hub2.start()
    try:
        rep = await hub2.maintenance.run_all(); print("  ok", rep.counts)
    except Exception as e: print("  maintenance ->", type(e).__name__, e)
    print("  service-a identity standing (should be good):", (await hub2.space("s").identities.get(ia)).standing)

async def main():
    await section("S1 refresh_many starved by provisioning identities", s1_starvation())
    await section("S2 probe raising", s2_probe_raises())
    await section("S3 partial Probes / naive datetime / empty targets", s3_only_one_method_and_misc())
    await section("S4 probe slower than 40s", s4_slow_probe())
    await section("S5 standing timers starved by frozen-until rows", s5_timer_starvation())
    await section("S6 override verification / standing key", s6_override_verification_and_key())
    await section("S7 identity of unregistered service poisons maintenance", s7_removed_service_poisons_maintenance())
asyncio.run(main())
