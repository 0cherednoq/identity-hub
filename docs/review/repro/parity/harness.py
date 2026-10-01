"""Общий каркас сравнения MemoryStore и PgStore (только чтение репозитория)."""

from __future__ import annotations

import asyncio
import sys
import traceback
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from identity_hub._ids import new_id
from identity_hub.clock import FakeClock
from identity_hub.crypto.envelope import EncryptedPayload
from identity_hub.postgres.store import PgStore
from identity_hub.store.base import NewIdentity, ServiceRow
from identity_hub.testing import MemoryStore

import os
PG_URL = os.environ.get("IH_PARITY_PG_URL", "postgresql+asyncpg://ih:ih@127.0.0.1:20728/ih")
PAYLOAD = EncryptedPayload("k1", b"w" * 8, b"n" * 12, b"c" * 16)


def ident(ref: str, *, id: UUID | None = None, service: str = "svc", **kw: Any) -> NewIdentity:
    base: dict[str, Any] = dict(
        id=id or new_id(),
        service=service,
        external_ref=ref,
        lifecycle="ready",
        standing="good",
        auth_state="authenticated",
        tags=[],
        attributes={},
        profile={},
        labels={},
        note=None,
        search_text=ref,
    )
    base.update(kw)
    return NewIdentity(**base)


async def pg_engine():
    from identity_hub.postgres import metadata
    from identity_hub.postgres.migrate import migrate

    engine = create_async_engine(PG_URL, pool_size=5, max_overflow=20)
    await migrate(engine)
    tables = ", ".join(t.name for t in metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    return engine


async def both_stores():
    clock = FakeClock(datetime.now(UTC))
    mem = MemoryStore(clock)
    engine = await pg_engine()
    pg = PgStore(async_sessionmaker(engine, expire_on_commit=False))
    for s in (mem, pg):
        await s.sync_services([ServiceRow("svc", "Service"), ServiceRow("svc2", "Service2")])
    return mem, pg, clock, engine


async def run_scenarios(scenarios, *, argv=None):
    from identity_hub.postgres import metadata

    engine = await pg_engine()
    tables = ", ".join(t.name for t in metadata.sorted_tables)
    wanted = set((argv or sys.argv)[1:])
    for fn in scenarios:
        if wanted - {"-v"} and fn.__name__ not in wanted:
            continue
        out = {}
        for kind in ("memory", "pg"):
            clock = None
            if kind == "memory":
                clock = FakeClock(datetime.now(UTC))
                store = MemoryStore(clock)
            else:
                async with engine.begin() as conn:
                    await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
                store = PgStore(async_sessionmaker(engine, expire_on_commit=False))
            await store.sync_services([ServiceRow("svc", "Service"), ServiceRow("svc2", "Service2")])
            space = await store.ensure_space("parity")
            try:
                out[kind] = await asyncio.wait_for(fn(store, space, clock), 30)
            except Exception as exc:  # noqa: BLE001
                out[kind] = f"EXC {type(exc).__name__}: {str(exc)[:200]}"
                if "-v" in wanted:
                    traceback.print_exc()
        same = out["memory"] == out["pg"]
        print()
        print(f"=== {fn.__name__}: {'SAME' if same else 'DIFF'}")
        print("  memory:", out["memory"])
        print("  pg    :", out["pg"])
    await engine.dispose()


__all__ = ["PAYLOAD", "ident", "run_scenarios", "both_stores", "pg_engine", "PG_URL", "timedelta", "uuid4"]
