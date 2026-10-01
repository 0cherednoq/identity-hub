import asyncio, socket, sys, ipaddress
from common import *
from identity_hub.dispatch.webhooks import validate_webhook_url
from identity_hub.errors import ValidationFailed

calls = []
_orig = socket.getaddrinfo


def spy(host, *a, **k):
    calls.append(host)
    raise AssertionError("DNS lookup attempted")  # no outbound lookups in this script


socket.getaddrinfo = spy

cases = [
    "https://10.0.0.5/hook",                 # literal private -> must be rejected
    "https://169.254.169.254/latest/",       # literal link-local
    "https://[::1]/x",
    "https://[::ffff:127.0.0.1]/x",          # IPv4-mapped
    "https://[::ffff:10.0.0.1]/x",
    "https://internal-db.corp.example/hook", # hostname that (in a real net) resolves to RFC1918
    "https://127.0.0.1.nip.io/hook",         # public wildcard DNS -> 127.0.0.1
    "https://metadata/computeMetadata/v1/",  # single-label search-domain name
    "https://2130706433/x",                  # decimal form of 127.0.0.1
    "https://0x7f000001/x",                  # hex form
    "https://127.1/x",                       # short form
    "https://0177.0.0.1/x",                  # octal form
    "https://localhost./x",
    "https://foo.internal/x",
    "https://kubernetes.default.svc/x",
    "https://host.docker.internal/x",
    "https://redis/x",
]
for url in cases:
    try:
        validate_webhook_url(url)
        verdict = "ACCEPTED"
    except ValidationFailed as e:
        verdict = f"rejected ({e})"
    print(f"{url:45s} {verdict}")
print("getaddrinfo calls made by validate_webhook_url:", calls)
socket.getaddrinfo = _orig
# How the OS resolver treats numeric shorthand (pure local parsing, AI_NUMERICHOST => no network)
for h in ("2130706433", "0x7f000001", "127.1", "0177.0.0.1"):
    try:
        r = socket.getaddrinfo(h, 443, type=socket.SOCK_STREAM, flags=socket.AI_NUMERICHOST)
        print(f"AI_NUMERICHOST {h!r} ->", r[0][4][0])
    except Exception as e:
        print(f"AI_NUMERICHOST {h!r} ->", type(e).__name__, e)
    try:
        print(f"   inet_aton {h!r} ->", socket.inet_ntoa(socket.inet_aton(h)))
    except Exception as e:
        print(f"   inet_aton {h!r} ->", type(e).__name__)


async def lease_token_in_error():
    from identity_hub.testing import make_service
    if "--pg" not in sys.argv:
        return
    hub, eng = await pg_hub([make_service("svc_a")])
    s1 = hub.space("s1", "S1"); s2 = hub.space("s2", "S2")
    await make_identity(s1, service="svc_a"); await make_identity(s2, service="svc_a")
    la = await s1.pools.define("pa", service="svc_a", holder="w").acquire(idempotency_key="job-42")
    try:
        await s2.pools.define("pa", service="svc_a", holder="w2").acquire(idempotency_key="job-42")
    except Exception as e:
        msg = str(e)
        print("---- IntegrityError text (truncated) ----")
        print(msg[:1500])
        import re
        uu = re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", msg)
        print("UUIDs in message:", len(uu), "| contains '[parameters:' =", "[parameters:" in msg)
    await eng.dispose()

asyncio.run(lease_token_in_error())
