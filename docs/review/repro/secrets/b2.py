import asyncio, sys, logging, io
from pydantic import BaseModel, SecretStr
from common import *
from identity_hub.errors import IdentityHubError
from identity_hub.importing import ImportRecord
from identity_hub.crypto import StaticSecretsProvider

logging.getLogger("identity_hub").setLevel(logging.CRITICAL)


async def b_httpx_log():
    print("== B: токен бота / URL вебхука в логах httpx (без сети: MockTransport)")
    import httpx
    from identity_hub.telegram_notify import HttpTelegramTransport
    buf = io.StringIO()
    h = logging.StreamHandler(buf); h.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    root = logging.getLogger(); root.addHandler(h); old = root.level; root.setLevel(logging.INFO)  # типичный logging.basicConfig(level=INFO)
    try:
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": True})))
        t = HttpTelegramTransport(client=client)
        await t.send_message(SecretStr("123456:AAE-LEAK-BOT-TOKEN"), 42, "hi")
        await client.aclose()
    finally:
        root.removeHandler(h); root.setLevel(old)
    out = buf.getvalue().strip()
    print("  лог приложения на уровне INFO:", out)
    print("  токен в логе:", "AAE-LEAK-BOT-TOKEN" in out)
    # исключение транспорта
    def boom(req): raise httpx.ConnectError("fail", request=req)
    client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    try:
        await HttpTelegramTransport(client=client).send_message(SecretStr("123456:AAE-LEAK-BOT-TOKEN"), 42, "hi")
    except Exception as e:
        print("  исключение транспорта:", type(e).__name__, e, "| __cause__:", e.__cause__, "| __context__ suppressed:", e.__suppress_context__, "| context has token:", "LEAK" in repr(getattr(e.__context__, "request", "")) )
    await client.aclose()


async def b_import():
    print("== B: импорт — что попадает в отчёт и наружу")
    class Creds(BaseModel):
        login: str
        password: SecretStr
    svc = make_service(credentials=Creds)
    hub = await mem_hub([svc]); space = hub.space("s1", "S1")
    recs = [
        ImportRecord("ok-1", credentials={"login": "u", "password": "PW"}),
        ImportRecord("bad-type", credentials={"login": "u", "password": {"nested": "LEAK-PW-IN-VALIDATION"}}),
        ImportRecord("bad-proxy-scheme", credentials={"login": "u", "password": "PW"}, proxy="ftp://user:LEAK-PROXY-PW@h:21"),
        ImportRecord("no-port", credentials={"login": "u", "password": "PW"}, proxy="http://user:LEAK-PROXY-PW2@h"),
    ]
    rep = await space.imports.run(service=svc, records=recs)
    print("  created:", len(rep.created), "| failed:")
    for ref, why in rep.failed:
        print("    ", ref, "->", why)
    print("  утечка в отчёте:", any("LEAK" in why for _, why in rep.failed))
    try:
        rep = await space.imports.run(service=svc, records=[
            ImportRecord("ok-2", credentials={"login": "u", "password": "PW"}),
            ImportRecord("slash-pw", credentials={"login": "u", "password": "PW"}, proxy="http://user:LEAKPW/tail@h:8080"),
            ImportRecord("ok-3", credentials={"login": "u", "password": "PW"}),
        ])
        print("  slash-pw: failed =", rep.failed, "created =", len(rep.created))
    except Exception as e:
        print("  пароль прокси с '/' ->", type(e).__name__, ":", e, "| IdentityHubError =", isinstance(e, IdentityHubError), "(весь импорт прерван)")
    try:
        await space.proxies.create("http://user:LEAKPW/tail@h:8080", group="g")
    except Exception as e:
        print("  proxies.create с тем же URL ->", type(e).__name__, ":", e)
    try:
        await space.proxies.create("http://user:pw@h:99999999", group="g")
    except Exception as e:
        print("  proxies.create порт вне диапазона ->", type(e).__name__, ":", e)
    try:
        await space.proxies.create("http://[user:LEAKPW@h:80", group="g")
    except Exception as e:
        print("  proxies.create битый IPv6 ->", type(e).__name__, ":", e)
    # staged import: что в ih_import_items.error
    run_id = await space.imports.stage(service=svc, source="upload.csv", records=[
        ImportRecord("st-1", credentials={"login": "u", "password": {"x": "LEAK-STAGED"}}),
    ])
    await hub.dispatcher.drain()
    info = await space.imports.get(run_id)
    print("  staged run:", info.state if hasattr(info, "state") else info)
    items = [v for k, v in hub.store._state.import_items.items()] if hasattr(hub.store._state, "import_items") else []
    for it in items:
        d = it if isinstance(it, dict) else getattr(it, "__dict__", {})
        print("    item state/error:", {k: d.get(k) for k in ("state", "error_code", "error", "payload") if k in d})


async def b_rotate_pg():
    print("== B: rotate-key на PostgreSQL")
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker
    class Creds(BaseModel):
        password: SecretStr
    svc = make_service(credentials=Creds)
    hub, eng = await pg_hub([svc])
    space = hub.space("s1", "S1")
    for i in range(5):
        await make_identity(space, credentials={"password": f"pw{i}"}, external_ref=f"r{i}")
    await space.proxies.create("socks5://u:p@10.0.0.1:1080", group="g")
    run_id = await space.imports.stage(service=svc, source="f", records=[ImportRecord("st-1", credentials={"password": "x"}), ImportRecord("st-2", credentials={"password": "y"})])
    K1, K2 = bytes(range(32)), b"\x22" * 32
    from identity_hub import IdentityHub
    sm = async_sessionmaker(eng, expire_on_commit=False)
    hub2 = IdentityHub(sm, StaticSecretsProvider({"k2": K2, "k1": K1}, "k2"), [svc]); await hub2.start()

    async def key_usage(label):
        async with eng.connect() as c:
            out = {}
            for t, col in [("ih_credentials", "key_id"), ("ih_sessions", "key_id"), ("ih_proxies", "key_id"), ("ih_import_items", "key_id")]:
                rows = (await c.execute(text(f"select {col}, count(*) from {t} group by 1 order by 1"))).all()
                out[t] = {r[0]: r[1] for r in rows}
        print(f"  {label}: {out}")

    await key_usage("до ротации")
    # 1) конкурентная блокировка строки: skip_locked -> CLI считает ротацию завершённой
    async with eng.connect() as locker:
        await locker.execute(text("begin")) if False else None
        tr = await locker.begin()
        await locker.execute(text("select 1 from ih_credentials order by identity_id limit 2 for update"))
        total = 0; batch = 500
        while True:
            rep = await hub2.doctor.rotate_key(limit=batch)
            total += rep.total
            if rep.total < batch:
                break
        print("  цикл CLI (batch=500) при 2 заблокированных строках: завершился, total =", total, rep.counts)
        await tr.rollback()
    await key_usage("после 'успешной' ротации с блокировкой")
    rep = await hub2.doctor.rotate_key(limit=500)
    print("  повторный запуск дорабатывает:", rep.counts)
    await key_usage("после второго запуска")
    # 2) воркер со старой конфигурацией продолжает писать под k1
    await make_identity(space, credentials={"password": "late"}, external_ref="late")   # hub (active=k1) — не перезапущенный воркер
    await key_usage("не перезапущенный воркер записал новую строку")
    # 3) оператор убирает k1
    hub3 = IdentityHub(sm, StaticSecretsProvider({"k2": K2}, "k2"), [svc]); await hub3.start()
    try:
        await hub3.doctor.rotate_key(limit=500); print("  rotate_key без k1: OK")
    except Exception as e:
        print("  rotate_key без k1 ->", type(e).__name__, e, "(одна нечитаемая строка блокирует весь пакет)")
    n = await hub3.dispatcher.drain()
    async with eng.connect() as c:
        rows = (await c.execute(text("select position, state, error_code, error from ih_import_items order by position"))).all()
    print("  staged import после удаления k1:", [tuple(r) for r in rows])
    await eng.dispose()


async def main():
    fns = [b_httpx_log, b_import]
    if "--pg" in sys.argv:
        fns.append(b_rotate_pg)
    for f in fns:
        try:
            await f()
        except Exception:
            import traceback; traceback.print_exc()

asyncio.run(main())
