import asyncio, sys, logging, pickle, dataclasses, base64, json, io
from dataclasses import dataclass
from datetime import timedelta
from pydantic import BaseModel, SecretStr
from common import *
from identity_hub.testing.memory import MemoryTx
from identity_hub.events import scrub, SecretLeak, EventDraft
from identity_hub.protocols import Fault, Blame
from identity_hub.crypto import parse_keys, StaticSecretsProvider, SecretValue, generate_key
from identity_hub.errors import IdentityHubError

logging.getLogger("identity_hub").setLevel(logging.CRITICAL)
SECRET_URL = "https://api.platform.example/v1/send?api_key=SK-LIVE-12345"


def events_of(hub, type_=None):
    return [(ev, ext) for ev, _, ext in hub.store._state.events if type_ is None or ev.type == type_]


async def b_lease_error():
    print("== B: текст исключения пользователя -> ih_lease_history.last_error и событие lease.failed")
    hub = await mem_hub(strict_events=True); space = hub.space("s1", "S1"); iid = await make_identity(space)
    pool = space.pools.define("p", service="test_service", holder="w")
    try:
        async with pool.lease() as lease:
            raise RuntimeError(f"Client error '401 Unauthorized' for url '{SECRET_URL}' via proxy socks5://user:PROXYPASS@1.2.3.4:1080")
    except RuntimeError:
        pass
    hist = await space._lease_history(iid)
    print("  lease_history.last_error:", hist[0].last_error)
    ev, ext = events_of(hub, "lease.failed")[0]
    print("  lease.failed data:", dict(ev.data), "| external =", ext, "| strict_events=True не сработал")
    page = await space._event_page(filters=__import__("identity_hub.read.events", fromlist=["EventFilters"]).EventFilters(include_journal=True, type="lease.failed"))
    print("  event_page(include_journal) data.error:", page.items[0].data.get("error"))


async def b_operation_error():
    print("== B: исключение в lease.operation -> счётчик last_error аренды (2000 симв.)")
    hub = await mem_hub(); space = hub.space("s1", "S1"); iid = await make_identity(space)
    pool = space.pools.define("p", service="test_service", holder="w")
    async with pool.lease() as lease:
        try:
            async with lease.operation("work"):
                raise ValueError(f"bad response for {SECRET_URL}")
        except ValueError:
            pass
    hist = await space._lease_history(iid)
    print("  end_reason:", hist[0].end_reason, "| last_error:", hist[0].last_error)
    ev, ext = events_of(hub, "lease.released")[0]
    print("  lease.released data.error:", ev.data.get("error"))
    ev, ext = events_of(hub, "operation.failed")[0]
    print("  operation.failed data:", dict(ev.data))


class Classifier:
    def classify(self, exc):
        return Fault(Blame.SESSION_INVALID, code="AUTH_KEY_UNREGISTERED", raw_status=str(exc))


async def b_classifier():
    print("== B: Fault.raw_status из классификатора -> ВНЕШНЕЕ событие identity.auth.changed")
    svc = make_service(error_classifier=Classifier())
    hub = await mem_hub([svc], strict_events=True); space = hub.space("s1", "S1"); iid = await make_identity(space)
    pool = space.pools.define("p", service="test_service", holder="w")
    async with pool.lease() as lease:
        try:
            async with lease.operation("work"):
                raise RuntimeError(f"401 for url {SECRET_URL} session=abcdef-SESSION-KEY")
        except RuntimeError:
            pass
    for ev, ext in events_of(hub, "identity.auth.changed"):
        print("  identity.auth.changed external =", ext, "| data.raw_status =", ev.data.get("raw_status"))
    card = await space._identity_card(iid)
    print("  card region raw_status:", [r.raw_status for r in card.regions if r.region == "auth"])


@dataclass
class Ctx:
    login: str
    password: str


async def b_scrub():
    print("== B: scrub — признаки и обходы")
    data = {
        "password": "p1", "bot_token": "t1", "AuthKey": "k0", "auth_key": "k1", "session_cookie": "c1",
        "api_key": "LEAK-api_key", "apikey": "LEAK-apikey", "pwd": "LEAK-pwd", "passphrase": "LEAK-passphrase",
        "authorization": "Bearer LEAK-authz", "session": "LEAK-session", "private_key": "LEAK-pk",
        "seed": "LEAK-seed", "pin": "LEAK-pin", "otp": "LEAK-otp", "code": "LEAK-2fa-code", "jwt": "LEAK-jwt",
        "access_key": "LEAK-ak", "sid": "LEAK-sid", "proxy": "socks5://u:LEAK-proxypass@h:1",
        "error": "boom password=LEAK-in-message", "raw_status": "LEAK-raw", "reason": "LEAK-reason",
        "note": "пароль LEAK-note", "nested": {"list": [{"k": "v", "password": "p2"}, "LEAK-in-list"]},
        "obj": Ctx("u", "LEAK-dataclass-str"), "typed": SecretStr("x"), "bytes": b"x",
        "model": make_service(), "tokens_used": 15, "evidence_count": 3, "password_changed_at": "2026-01-01",
    }
    out = scrub(data)
    leaked = sorted(k for k, v in out.items() if "LEAK" in json.dumps(v, ensure_ascii=False, default=str))
    print("  прошли без вырезания:", leaked)
    print("  вырезаны (ложные срабатывания тоже):", sorted(k for k, v in out.items() if v == "[redacted]"))
    print("  nested:", out["nested"], "| obj:", out["obj"], "| model:", out["model"] if isinstance(out["model"], str) and len(out["model"]) < 60 else str(out["model"])[:80])
    try:
        scrub({"api_key": "x", "error": "password=1"}, strict=True); print("  strict: api_key/error НЕ считаются утечкой")
    except SecretLeak as e:
        print("  strict:", e)
    try:
        scrub({"password_changed_at": "2026"}, strict=True)
    except SecretLeak as e:
        print("  strict ложное срабатывание:", e)


async def b_sink():
    print("== B: LeaseLogSink — фильтрации нет, поля без лимита")
    hub = await mem_hub(); space = hub.space("s1", "S1"); iid = await make_identity(space)
    root = logging.getLogger("userapp"); root.addHandler(hub.lease_log_sink); root.setLevel(logging.INFO)
    third = logging.getLogger("urllib3.connectionpool"); third.addHandler(hub.lease_log_sink)
    pool = space.pools.define("p", service="test_service", holder="w")
    captured = []
    orig = MemoryTx.insert_lease_logs
    async def spy(self, rows):
        captured.extend(rows); return await orig(self, rows)
    MemoryTx.insert_lease_logs = spy
    try:
        async with pool.lease() as lease:
            root.warning("login failed for %s with password %s", "user", "LEAK-log-password", extra={"proxy": lease.proxy or "socks5://u:LEAK-extra@h:1", "blob": "A" * 5_000_000, "session": {"auth_key": "LEAK-nested-extra"}})
            third.warning("Retrying after connection broken: /bot123456:LEAK-bot-token/sendMessage")
            try:
                raise RuntimeError("LEAK-in-traceback")
            except RuntimeError:
                root.exception("failure")
            for i in range(300):
                root.warning("m%d", i)
    finally:
        MemoryTx.insert_lease_logs = orig
    print("  записано строк:", len(captured), "(лимит max_per_lease=200)")
    print("  msg[0]:", captured[0]["message"])
    print("  fields[0] keys:", {k: (v if len(str(v)) < 60 else f"<{len(str(v))} chars>") for k, v in captured[0]["fields"].items()})
    print("  msg[1] (сторонний логгер):", captured[1]["message"])
    print("  msg[2] (exception):", captured[2]["message"], "| traceback в полях:", any("LEAK-in-traceback" in str(v) for v in captured[2]["fields"].values()))
    print("  остаток в sink после release: buffers =", len(hub.lease_log_sink._buffers), "counts =", len(hub.lease_log_sink._counts))
    l = await pool.acquire()
    tok = logging.getLogger("userapp")
    from identity_hub.context import _current
    t = _current.set(l.context); tok.warning("x"); _current.reset(t)
    print("  sink для аренды без release (pool.acquire): buffers =", len(hub.lease_log_sink._buffers), "(не очищается до release)")


async def b_reveal():
    print("== B: secrets.reveal")
    class Creds(BaseModel):
        login: str
        password: SecretStr
    svc = make_service(credentials=Creds)
    hub = await mem_hub([svc]); space = hub.space("s1", "S1")
    iid = await make_identity(space, credentials={"login": "u", "password": "PW-1"})
    r = await space.secrets.reveal(iid, "credentials", actor="admin")
    print("  reveal без reason -> тип", type(r).__name__, r, "| событие:", dict(events_of(hub, "identity.secret.revealed")[-1][0].data))
    for actor in ("", " ", "x"):
        try:
            await space.secrets.reveal(iid, "credentials", actor=actor); print(f"  actor={actor!r}: принят")
        except IdentityHubError as e:
            print(f"  actor={actor!r}: {type(e).__name__}")
    n_before = len(events_of(hub, "identity.secret.revealed"))
    orig = MemoryTx.emit
    async def boom(self, drafts): raise ConnectionError("audit write failed")
    MemoryTx.emit = boom
    try:
        got = await space.secrets.reveal(iid, "credentials", actor="admin", reason="r"); print("  АУДИТ УПАЛ, секрет получен:", got)
    except ConnectionError as e:
        print("  сбой записи аудита -> секрет не выдан:", e)
    finally:
        MemoryTx.emit = orig
    print("  событий аудита:", n_before, "->", len(events_of(hub, "identity.secret.revealed")))
    # во время чужой аренды
    pool = space.pools.define("p", service="test_service", holder="w")
    async with pool.lease() as lease:
        r = await space.secrets.reveal(iid, "credentials", actor="admin", reason="r")
        print("  reveal при живой чужой аренде:", "выдан" if r else "нет")
    # strict_events + reason с секретом
    ev, ext = events_of(hub, "identity.secret.revealed")[-1]
    print("  identity.secret.revealed external =", ext, "severity =", ev.severity)
    # обход аудита: публичный proxies.url
    pid = (await space.proxies.create("socks5://puser:PROXY-PW@10.1.1.1:1080", group="g")).id
    n = len(hub.store._state.events)
    url = await space.proxies.url(pid)
    print("  space.proxies.url() ->", url, "| новых событий аудита:", len(hub.store._state.events) - n)
    # прямой доступ
    async with space._tx(own=True) as tx:
        payload = await tx.get_credentials(iid)
    from identity_hub.crypto import associated_data
    print("  hub.envelope.open_model без аудита:", hub.envelope.open_model(Creds, payload, associated_data("ih_credentials", iid)).password.get_secret_value())
    print("  hub.envelope._provider.keys():", {k: f"<{len(v)} bytes>" for k, v in hub.envelope._provider.keys().items()}, "repr:", repr(hub.envelope._provider))


async def b_repr():
    print("== B: repr / pickle")
    class Creds(BaseModel):
        login: str
        password: str  # пользователь не использовал SecretStr
    svc = make_service(credentials=Creds)
    hub = await mem_hub([svc]); space = hub.space("s1", "S1")
    iid = await make_identity(space, credentials={"login": "u", "password": "PW-PLAIN"})
    px = await space.proxies.create("socks5://puser:PROXY-PW@10.1.1.1:1080", group="g"); print("  repr(Proxy):", repr(px)[:160])
    from identity_hub.service import ProxyPolicy
    pool = space.pools.define("p", service="test_service", holder="w", proxy_policy=ProxyPolicy(group="g"))
    lease = await pool.acquire()
    print("  repr(lease):", repr(lease))
    print("  repr(lease._row) содержит token:", str(lease._token) in repr(lease._row))
    print("  lease.proxy:", type(lease.proxy).__name__, lease.proxy)
    print("  repr(lease.proxy_info) содержит пароль:", "PROXY-PW" in repr(lease.proxy_info), "|", repr(lease.proxy_info.secret))
    creds = await lease.credentials()
    print("  repr(credentials):", repr(creds), "| model_dump:", creds.model_dump())
    try:
        blob = pickle.dumps(lease._row); print("  pickle(LeaseRow) OK, token внутри:", lease._token.bytes in blob or str(lease._token).encode() in blob or lease._token.int.to_bytes(16, 'big') in blob)
    except Exception as e:
        print("  pickle(LeaseRow):", type(e).__name__)
    try:
        blob = pickle.dumps(hub.envelope._provider); print("  pickle(StaticSecretsProvider) OK, ключ внутри:", bytes(range(32)) in blob)
    except Exception as e:
        print("  pickle(provider):", type(e).__name__)
    try:
        pickle.dumps(lease); print("  pickle(lease) OK")
    except Exception as e:
        print("  pickle(lease):", type(e).__name__, str(e)[:80])
    from identity_hub import IdentityCreate
    ic = IdentityCreate(service="test_service", external_ref="x", credentials={"password": "PW"})
    print("  repr(IdentityCreate):", repr(ic), "| asdict содержит пароль:", "PW" in str(dataclasses.asdict(ic)))
    from identity_hub.importing import ImportRecord
    print("  repr(ImportRecord):", repr(ImportRecord("ref", credentials={"password": "PW-IMPORT"}, proxy="http://u:PX@h:1"))[:200])
    card = await space._identity_card(iid)
    print("  IdentityCard.identity.profile_secret:", card.identity.profile_secret, "| lease в карточке:", card.lease)
    await lease.release()
    sv = SecretValue("s3cr3t")
    print("  SecretValue: str=%s repr=%r f=%s" % (sv, sv, f"{sv}"), "| json:", end=" ")
    try:
        json.dumps({"a": sv})
    except TypeError as e:
        print("TypeError", "| pickle:", end=" ")
    try:
        pickle.dumps(sv)
    except TypeError as e:
        print("TypeError")
    print("  scrub(SecretValue):", scrub({"x": sv}))


async def b_keys():
    print("== B: провайдеры ключей")
    good = generate_key("k1")
    b64 = good.split(":", 1)[1]
    for label, spec in [("перепутан порядок <b64>:<id>", f"{b64}:k1"), ("31 байт", "k1:" + base64.b64encode(b"x" * 31).decode()),
                        ("без id", b64), ("пробел в середине", f"k1: {b64}"), ("BOM", "﻿" + good),
                        ("ключ из нулей", "k1:" + base64.b64encode(bytes(32)).decode()), ("id 300 симв.", "k" * 300 + ":" + b64),
                        ("url-safe b64", "k1:" + base64.urlsafe_b64encode(bytes([251] * 32)).decode())]:
        try:
            active, keys = parse_keys(spec)
            print(f"  {label}: принято, active={active[:20]!r} len(id)={len(active)}")
        except IdentityHubError as e:
            msg = str(e)
            print(f"  {label}: {type(e).__name__}: {msg[:70]}{'...' if len(msg) > 70 else ''} | материал ключа в сообщении: {b64 in msg}")
    p = StaticSecretsProvider({"k1": bytes(range(32))}, "k1")
    p.keys()["k1"] = b"\0" * 32
    print("  keys() отдаёт внутренний изменяемый dict: ключ подменён =", p.keys()["k1"] == b"\0" * 32)
    import os, tempfile, stat
    from identity_hub.crypto import FileSecretsProvider, EnvSecretsProvider
    d = tempfile.mkdtemp(dir=os.path.dirname(__file__)); path = os.path.join(d, "kek"); open(path, "w").write(good)
    fp = FileSecretsProvider(path)
    k2 = generate_key("k2"); open(path, "w").write(k2 + "\n" + good)
    print("  FileSecretsProvider после изменения файла: active =", fp.active_key_id(), "keys =", sorted(fp.keys()), "(перечитывания нет)")
    os.environ["IDENTITY_HUB_KEK"] = good
    ep = EnvSecretsProvider()
    print("  EnvSecretsProvider: переменная остаётся в os.environ =", "IDENTITY_HUB_KEK" in os.environ)
    del os.environ["IDENTITY_HUB_KEK"]


async def main():
    for f in (b_reveal, b_repr):
        try:
            await f()
        except Exception:
            import traceback; traceback.print_exc()

asyncio.run(main())
