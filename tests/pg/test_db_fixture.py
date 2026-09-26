"""P0.3: каждый тест получает пустые таблицы."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.pg


@pytest.mark.parametrize("run", [1, 2])
async def test_tables_are_clean_between_tests(db: AsyncEngine, run: int) -> None:
    async with db.begin() as conn:
        count = (await conn.execute(text("SELECT count(*) FROM ih_spaces"))).scalar_one()
        assert count == 0
        await conn.execute(
            text("INSERT INTO ih_spaces (id, slug, title) VALUES (gen_random_uuid(), 's', 'S')")
        )
