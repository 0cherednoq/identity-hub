import asyncio, sys, traceback
from datetime import timedelta, datetime, UTC
from pydantic import BaseModel, SecretStr
from identity_hub import IdentityHub, StaticSecretsProvider, Override, LoginChallenge, ProbeResult, Service, OperationRule
from identity_hub.clock import FakeClock
from identity_hub.testing import MemoryStore, make_identity, make_service

class Sess(BaseModel):
    auth_key: str

def mkhub(services, clock=None, **kw):
    clock = clock or FakeClock()
    store = MemoryStore(clock)
    hub = IdentityHub(store=store, secrets=StaticSecretsProvider({"k": bytes(range(32))}, active="k"), services=services, clock=clock, **kw)
    return hub, clock

async def auth_of(space, iid):
    return str((await space.identities.get(iid)).auth)

async def section(name, coro):
    print(f"\n=== {name}")
    try:
        await asyncio.wait_for(coro, 40)
    except BaseException as exc:
        print("  !! EXC", type(exc).__name__, str(exc)[:300])
