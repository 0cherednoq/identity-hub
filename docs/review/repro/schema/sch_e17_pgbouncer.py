"""E17: рецепт docs/integrations.md для PgBouncer transaction pooling; E18: глобальный PK ih_operation_keys."""

from __future__ import annotations

import asyncio
import sys
from collections import Counter
from uuid import uuid4

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from identity_hub.api.identities import IdentityCreate
from identity_hub.crypto import StaticSecretsProvider
from identity_hub.hub import IdentityHub
from identity_hub.service import Service
from sch_common import run, url

PGB_PORT = int(sys.argv[1])
SVC = Service(name="svc", title="Svc")


async def workload(connect_args: dict, label: str) -> None:  # type: ignore[type-arg]
    engine = create_async_engine(
        f"postgresql+asyncpg://postgres:review@127.0.0.1:{PGB_PORT}/sch_e6_ihs",
        connect_args=connect_args,
        pool_pre_ping=True,
        pool_size=10,
    )
    hub = IdentityHub(async_sessionmaker(engine, expire_on_commit=False),
                      StaticSecretsProvider({"k1": b"\x11" * 32}, "k1"), [SVC], db_schema="ihs")
    errors: Counter[str] = Counter()
    ok = 0
    try:
        await hub.start()
        space = hub.space("rev")

        async def one(i: int) -> None:
            nonlocal ok
            try:
                await space.identities.ensure(IdentityCreate(service="svc", external_ref=f"pgb-{label}-{i % 5}"))
                await space.identities.search("pgb")
                ok += 1
            except Exception as exc:
                errors[f"{type(exc).__name__}: {(str(exc) or repr(exc)).splitlines()[0][:200]}"] += 1

        for _ in range(3):
            await asyncio.gather(*(one(i) for i in range(20)))
        async with hub.store.listen("identity_hub_events") as signal:
            print(f"[{label}] listen() returned signal: {signal is not None}")
    except Exception as exc:
        errors[f"start: {type(exc).__name__}: {(str(exc) or repr(exc)).splitlines()[0][:200]}"] += 1
    print(f"[{label}] ok={ok} errors={dict(errors)}")
    await engine.dispose()


async def op_keys() -> None:
    engine = create_async_engine(url("sch_e6_ihs"))
    hub = IdentityHub(async_sessionmaker(engine, expire_on_commit=False),
                      StaticSecretsProvider({"k1": b"\x11" * 32}, "k1"), [SVC], db_schema="ihs")
    await hub.start()
    a, b = hub.space("opk-a"), hub.space("opk-b")
    ia = await a.identities.create(IdentityCreate(service="svc", external_ref=f"opk-a-{uuid4()}"))
    ib = await b.identities.create(IdentityCreate(service="svc", external_ref=f"opk-b-{uuid4()}"))
    key = f"order-{uuid4()}"
    async with a._tx(own=True) as tx:
        first = await tx.charge_quotas(ia.id, [], operation_key=key, kind="send")
    async with b._tx(own=True) as tx:
        second = await tx.charge_quotas(ib.id, [], operation_key=key, kind="send")
    print("E18 same operation key, space A:", first.duplicate, "| другой space и identity:", second.duplicate)
    await engine.dispose()


async def main() -> None:
    await workload({"prepared_statement_cache_size": 0}, "docs recipe")
    await workload(
        {
            "prepared_statement_cache_size": 0,
            "statement_cache_size": 0,
            "prepared_statement_name_func": lambda: f"__asyncpg_{uuid4()}__",
        },
        "full recipe",
    )
    await op_keys()


if __name__ == "__main__":
    run(main())
