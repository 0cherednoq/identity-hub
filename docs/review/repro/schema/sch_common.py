"""Общие помощники ревью схемы: отдельная база на каждый эксперимент в контейнере ih-schema-review-pg."""

from __future__ import annotations

import asyncio
import sys

import asyncpg
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

PORT = 1390
ADMIN = f"postgresql://postgres:review@127.0.0.1:{PORT}/postgres"


def url(db: str, user: str = "postgres", password: str = "review") -> str:
    return f"postgresql+asyncpg://{user}:{password}@127.0.0.1:{PORT}/{db}"


async def fresh_db(name: str) -> AsyncEngine:
    conn = await asyncpg.connect(ADMIN)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()
    return create_async_engine(url(name), pool_size=10, max_overflow=40)


async def admin_exec(*statements: str, db: str = "postgres") -> None:
    conn = await asyncpg.connect(ADMIN.rsplit("/", 1)[0] + "/" + db)
    try:
        for s in statements:
            await conn.execute(s)
    finally:
        await conn.close()


def run(coro) -> None:  # type: ignore[no-untyped-def]
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(coro)
