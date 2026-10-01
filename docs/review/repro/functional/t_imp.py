from common import *
from identity_hub.importing import ImportRecord
from pathlib import Path

class Creds(BaseModel):
    phone: str
    password: SecretStr

def svc_():
    return make_service("svc", credentials=Creds, session=Sess)

async def e1_bad_proxy():
    svc = svc_(); hub, clock = mkhub([svc]); await hub.start(); sp = hub.space("s")
    recs = [ImportRecord("a1"), ImportRecord("a2"), ImportRecord("a3", proxy="http://h:abc"), ImportRecord("a4")]
    try:
        rep = await sp.imports.run(service=svc, records=recs, batch_size=2); print("  report", rep)
    except Exception as e: print("  imports.run ->", type(e).__name__, e)
    print("  identities committed despite exception (report lost):", await sp.identities.count())

async def e2_bad_link():
    svc = svc_(); hub, clock = mkhub([svc]); await hub.start(); sp = hub.space("s")
    recs = [ImportRecord(f"b{i}") for i in range(5)] + [ImportRecord("b5", links={"email": "nope"})]
    rep = await sp.imports.run(service=svc, records=recs)
    print("  created:", len(rep.created), "failed:", len(rep.failed), rep.failed[0][1][:110])

async def e3_stage_update():
    svc = svc_(); hub, clock = mkhub([svc]); await hub.start(); sp = hub.space("s")
    iid = await make_identity(sp, service=svc, external_ref="c1", authenticated=False)
    run = await sp.imports.stage(service=svc, records=[ImportRecord("c1", sessions={"default": {"auth_key": "new"}})], source="f.csv", on_existing="update_session", verify_sessions=True)
    print("  runner processed:", await hub.import_runner.run_once())
    info = await sp.imports.get(run)
    print("  run state:", info.state, "created/updated/skipped/failed:", info.created, info.updated, info.skipped, info.failed)
    print("  report:", getattr(info, "report", None))
    print("  identity auth actually:", await auth_of(sp, iid), "(verify_sessions=True was requested)")

async def e4_stage_poison():
    svc = svc_(); hub, clock = mkhub([svc]); await hub.start(); sp = hub.space("s")
    run1 = await sp.imports.stage(service=svc, records=[ImportRecord("d1", proxy="http://h:abc")], source="bad")
    run2 = await sp.imports.stage(service=svc, records=[ImportRecord("d2")], source="good")
    for i in range(3):
        try: print("  run_once:", await hub.import_runner.run_once())
        except Exception as e: print("  run_once ->", type(e).__name__, e)
        clock.advance(timedelta(minutes=6))
    print("  run1:", (await sp.imports.get(run1)).state, "run2:", (await sp.imports.get(run2)).state)
    try: print("  dispatcher.drain:", await asyncio.wait_for(hub.dispatcher.drain(), 5))
    except Exception as e: print("  dispatcher.drain ->", type(e).__name__, e)

async def e5_report_content():
    svc = svc_(); hub, clock = mkhub([svc]); await hub.start(); sp = hub.space("s")
    rep = await sp.imports.run(service=svc, records=[ImportRecord("+79990001122", credentials={"phone": "+79990001122", "password": {"oops": "Sup3rSecret"}}),
                                                       ImportRecord("x2", credentials={"phone": 1})])
    print("  failed:", rep.failed)
    run = await sp.imports.stage(service=svc, records=[ImportRecord("+79990001133", credentials={"phone": ["Sup3rSecret"]})], source="s")
    await hub.import_runner.run_once()
    info = await sp.imports.get(run); print("  staged report:", info)

async def e6_files():
    seen = []
    class Imp:
        def can_import(self, path): return True
        async def read(self, path):
            seen.append(str(path)); yield ImportRecord(path.name)
    svc = make_service("svc", importers=(Imp(),))
    hub, clock = mkhub([svc]); await hub.start(); sp = hub.space("s")
    await sp.imports.run_files(service=svc, paths=["../../../../etc/passwd", "C:/Windows/win.ini"])
    print("  importer received paths as-is:", seen)
    class Sync:
        def can_import(self, path): return True
        def read(self, path): return [ImportRecord("z")]
    svc2 = make_service("svc2", importers=(Sync(),)); hub.register_service(svc2)
    try: await sp.imports.run_files(service=svc2, paths=["a"])
    except Exception as e: print("  importer returning list ->", type(e).__name__, e)

async def main():
    await section("E1 bad proxy url aborts import", e1_bad_proxy())
    await section("E2 one bad link rejects whole batch", e2_bad_link())
    await section("E3 staged update_session", e3_stage_update())
    await section("E4 staged poison record", e4_stage_poison())
    await section("E5 report content", e5_report_content())
    await section("E6 file importers", e6_files())
asyncio.run(main())
