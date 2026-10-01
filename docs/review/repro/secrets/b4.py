import asyncio, logging, dataclasses
from pydantic import BaseModel, SecretStr
from common import *
from identity_hub.errors import IdentityHubError

logging.getLogger("identity_hub").setLevel(logging.CRITICAL)


class Creds(BaseModel):
    password: SecretStr


async def main():
    svc = make_service(credentials=Creds)
    hub = await mem_hub([svc]); space = hub.space("s1", "S1")
    iid = await make_identity(space, credentials={"password": "PW-1"}, session={"auth_key": "AK-1"})
    pool = space.pools.define("p", service="test_service", holder="w")

    lease = await pool.acquire()
    await lease.release()
    try:
        c = await lease.credentials(); s = await lease.session()
        print("после release(): credentials ->", c.password.get_secret_value(), "| session ->", s)
    except IdentityHubError as e:
        print("после release():", type(e).__name__, e)

    async with pool.lease() as l2:
        pass
    try:
        c = await l2.credentials()
        print("после выхода из async with: credentials ->", c.password.get_secret_value())
    except IdentityHubError as e:
        print("после выхода из async with:", type(e).__name__, e)

    lease = await pool.acquire()
    revoked = await space.pools.revoke(iid, reason="admin", actor="admin") if hasattr(space.pools, "revoke") else await space._revoke_lease(iid, reason="admin", actor="admin")
    try:
        c = await lease.credentials()
        print("после revoke (до heartbeat): revoked =", revoked, "| credentials ->", c.password.get_secret_value())
    except IdentityHubError as e:
        print("после revoke:", type(e).__name__, e)
    # другой воркер уже взял аккаунт
    other = await pool.acquire()
    try:
        c = await lease.credentials()
        print("аккаунт уже у другого воркера (fencing %d vs %d): старый lease.credentials() ->" % (lease.fencing, other.fencing), c.password.get_secret_value())
    except IdentityHubError as e:
        print("stale lease:", type(e).__name__, e)
    px = await space.proxies.create("socks5://puser:PROXY-PW@10.1.1.1:1080", group="g")
    print("Proxy fields:", [f.name for f in dataclasses.fields(px)])

asyncio.run(main())
