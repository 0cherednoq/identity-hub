from common import *

async def a1_wrong_session():
    class A:
        async def login(self, ctx): return {"wrong": 1}
        async def refresh(self, c, s): return None
    svc = make_service("svc", session=Sess, authenticator=A())
    hub, clock = mkhub([svc]); await hub.start(); sp = hub.space("s")
    iid = await make_identity(sp, service=svc, authenticated=False)
    print("  outcome:", await sp.auth.login(iid), "auth:", await auth_of(sp, iid))
    try:
        print("  second:", await sp.auth.login(iid))
    except Exception as e: print("  second login ->", type(e).__name__, e)
    rep = await sp.auth.login_many(service=svc)
    print("  login_many:", rep)

async def a2_slow_login():
    holder = {}
    class A:
        async def login(self, ctx):
            holder["clock"].advance(45)   # вход занял 45 с
            return Sess(auth_key="k")
        async def refresh(self, c, s): return None
    svc = make_service("svc", session=Sess, authenticator=A())
    hub, clock = mkhub([svc]); holder["clock"] = clock; await hub.start(); sp = hub.space("s")
    iid = await make_identity(sp, service=svc, authenticated=False)
    print("  outcome:", await sp.auth.login(iid), "auth:", await auth_of(sp, iid))

async def a3_cancel():
    class A:
        async def login(self, ctx):
            await asyncio.sleep(3600)
        async def refresh(self, c, s): return None
    svc = make_service("svc", session=Sess, authenticator=A())
    hub, clock = mkhub([svc]); await hub.start(); sp = hub.space("s")
    iid = await make_identity(sp, service=svc, authenticated=False)
    try:
        await asyncio.wait_for(sp.auth.login(iid), 0.3)
    except asyncio.TimeoutError: print("  timeout (caller cancelled)")
    print("  auth:", await auth_of(sp, iid))
    card = await sp.identities.card(iid)
    print("  lease after cancel:", getattr(card, "lease", "n/a"))

async def a4_override_then_challenge():
    class A:
        async def login(self, ctx):
            if ctx.challenge_answer is None:
                raise LoginChallenge("sms", "code", {"h": 1})
            return Sess(auth_key="k")
        async def refresh(self, c, s): return None
    svc = make_service("svc", session=Sess, authenticator=A())
    hub, clock = mkhub([svc]); await hub.start(); sp = hub.space("s")
    iid = await make_identity(sp, service=svc, authenticated=False)
    print("  1st:", await sp.auth.login(iid), await auth_of(sp, iid))
    # оператор «перезапускает» вход рекомендованным в limitations.md override
    await sp.identities.override(iid, Override("auth", "default", "no_session", reason="restart", actor="op"))
    print("  after override:", await auth_of(sp, iid))
    print("  2nd:", await sp.auth.login(iid), await auth_of(sp, iid))
    card = await sp.identities.card(iid)
    print("  open_challenge:", card.open_challenge)
    ch = card.open_challenge
    try:
        print("  answer old challenge:", await asyncio.wait_for(sp.auth.answer_challenge(__import__('uuid').UUID(str(ch["id"])), "1"), 3))
    except BaseException as e: print("  answer ->", type(e).__name__, str(e)[:200])
    print("  auth:", await auth_of(sp, iid))

async def a5_bad_adapters():
    class NoLogin: pass
    class SyncLogin:
        def login(self, ctx): return Sess(auth_key="k")
    for A in (NoLogin, SyncLogin):
        try:
            svc = make_service("svc", session=Sess, authenticator=A())
        except Exception as e:
            print("  declared ->", type(e).__name__, e); continue
        hub, clock = mkhub([svc]); await hub.start(); sp = hub.space("s")
        iid = await make_identity(sp, service=svc, authenticated=False)
        rep = await sp.auth.login_many(service=svc)
        print(" ", A.__name__, "declared OK; login_many:", rep.failed, "auth:", await auth_of(sp, iid))
    from identity_hub.protocols import Authenticator
    print("  isinstance(NoLogin(), Authenticator):", isinstance(NoLogin(), Authenticator))
    class OnlyLogin:
        async def login(self, ctx): ...
    print("  isinstance(OnlyLogin(), Authenticator):", isinstance(OnlyLogin(), Authenticator))

async def main():
    await section("A1 authenticator returns wrong session shape", a1_wrong_session())
    await section("A2 login longer than ttl-safety_margin (40s)", a2_slow_login())
    await section("A3 caller cancels/timeouts a hanging login", a3_cancel())
    await section("A4 override->no_session while challenge open, then login", a4_override_then_challenge())
    await section("A5 invalid adapters", a5_bad_adapters())
asyncio.run(main())
