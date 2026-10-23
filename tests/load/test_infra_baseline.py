"""I6.4: базовая латентность окружения — сколько стоит round trip и параллельная транзакция."""

from __future__ import annotations

import asyncio
import time

import pytest
from benchmarks.run import summarize
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.load


async def test_round_trip_and_parallel_transactions(db: AsyncEngine) -> None:
    async def one_select() -> float:
        started = time.perf_counter()
        async with db.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return time.perf_counter() - started

    async def one_transaction() -> float:
        started = time.perf_counter()
        async with db.begin() as conn:
            for _ in range(9):  # столько же запросов, сколько делает acquire
                await conn.execute(text("SELECT 1"))
        return time.perf_counter() - started

    print(f"INFRA single select {summarize([await one_select() for _ in range(10)])}")
    print(f"INFRA single tx x9 {summarize([await one_transaction() for _ in range(5)])}")
    for level in (1, 8, 32, 64):
        samples = await asyncio.gather(*(one_transaction() for _ in range(level)))
        print(f"INFRA parallel tx level={level} {summarize(samples)}")
