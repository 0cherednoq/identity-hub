import asyncio, logging, sys
from datetime import timedelta
from identity_hub import IdentityHub, StaticSecretsProvider
from identity_hub.testing import MemoryStore, make_identity, make_service

PG_URL = "postgresql+asyncpg://postgres:ihreview@127.0.0.1:2749/ih"

async def mem_hub(services=None, **kw):
    services = services or [make_service()]
    hub = IdentityHub(store=MemoryStore(), secrets=StaticSecretsProvider({"k1": bytes(range(32))}, active="k1"),
                      services=services, **kw)
    await hub.start()
    return hub

async def pg_hub(services=None, truncate=True, **kw):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from identity_hub.postgres.migrate import migrate
    from identity_hub.postgres import metadata
    eng = create_async_engine(PG_URL)
    await migrate(eng)
    if truncate:
        tables = ", ".join(t.name for t in metadata.sorted_tables)
        async with eng.begin() as conn:
            await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    services = services or [make_service()]
    hub = IdentityHub(async_sessionmaker(eng, expire_on_commit=False),
                      StaticSecretsProvider({"k1": bytes(range(32))}, "k1"), services, **kw)
    await hub.start()
    return hub, eng
