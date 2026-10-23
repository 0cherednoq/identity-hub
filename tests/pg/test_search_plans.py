"""I6.2 (`PERF-1`): ключевые формы поиска идут по индексам, а не читают таблицу целиком."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine

from identity_hub import IdentityCreate, IdentityHub, Lifecycle
from identity_hub.hub import SpaceHandle
from identity_hub.read.search import AccountSearchOptions

pytestmark = pytest.mark.pg


async def _seed(space: SpaceHandle, db: AsyncEngine, count: int = 400) -> None:
    await space.identities.create_many(
        [
            IdentityCreate(
                service="fake_tg",
                external_ref=f"+7911{index:06d}",
                labels={"farm": str(index % 7)},
                profile={"country": "UA" if index % 3 else "PL"},
                lifecycle=Lifecycle.READY,
            )
            for index in range(count)
        ],
        actor="test",
    )
    async with db.begin() as conn:
        await conn.execute(text("ANALYZE ih_identities"))


async def _plans(hub: IdentityHub, db: AsyncEngine, run: Any) -> list[tuple[str, str]]:
    captured: list[tuple[str, Any]] = []

    def capture(_conn: Any, _cursor: Any, statement: str, parameters: Any, *_rest: Any) -> None:
        captured.append((statement, parameters))

    event.listen(db.sync_engine, "before_cursor_execute", capture)
    try:
        await run()
    finally:
        event.remove(db.sync_engine, "before_cursor_execute", capture)

    plans: list[tuple[str, str]] = []
    async with db.connect() as conn:
        # план проверяется при выключенном переборе: так тест говорит «путь по индексу есть
        # и применим», а не сравнивает стоимости на объёме тестовых данных
        await conn.exec_driver_sql("SET enable_seqscan = off")
        for statement, parameters in captured:
            if "ih_identities" not in statement or "set_config" in statement:
                continue
            rows = await conn.exec_driver_sql("EXPLAIN " + statement, parameters)
            plans.append((statement, "\n".join(str(row[0]) for row in rows)))
        await conn.exec_driver_sql("SET enable_seqscan = on")
    return plans


async def test_default_list_page_uses_keyset_index(
    hub: IdentityHub, db: AsyncEngine, space: SpaceHandle
) -> None:
    """R2-D09: страница по умолчанию (created_at) идёт по индексу, в том числе со курсором."""
    await _seed(space, db)
    first = await space.admin.identity_page(limit=50, count=False)
    assert first.next_cursor is not None

    plans = await _plans(
        hub, db, lambda: space.admin.identity_page(limit=50, after=first.next_cursor, count=False)
    )
    paged = [(statement, plan) for statement, plan in plans if "LIMIT" in statement.upper()]
    assert paged, [statement for statement, _ in plans]
    for statement, plan in paged:
        assert "Seq Scan on ih_identities" not in plan, f"{statement}\n{plan}"


async def test_key_search_forms_avoid_table_scan(
    hub: IdentityHub, db: AsyncEngine, space: SpaceHandle
) -> None:
    """Ни одна ключевая форма фильтра не читает ih_identities перебором."""
    await _seed(space, db)

    forms = (
        ("+7911000005", None),  # подстрока
        ("farm:3", None),  # метки
        ("country:UA", None),  # профиль
        ("", AccountSearchOptions(eligible=True)),  # регионы и пригодность
        ("", AccountSearchOptions(sort_by="error_score")),  # другая сортировка курсора
    )
    for query, options in forms:
        plans = await _plans(
            hub,
            db,
            lambda query=query, options=options: space.admin.identity_page(
                query, options=options, limit=20, count=False
            ),
        )
        paged = [(statement, plan) for statement, plan in plans if "LIMIT" in statement.upper()]
        assert paged, f"{query!r}: запрос страницы не найден"
        for statement, plan in paged:
            assert "Seq Scan on ih_identities" not in plan, f"{query!r} {statement} {plan}"


async def test_substring_search_has_trigram_index_path(
    hub: IdentityHub, db: AsyncEngine, space: SpaceHandle
) -> None:
    """Подстрочный поиск опирается на trigram-индекс search_text (`FR-11.1`)."""
    await _seed(space, db)
    async with db.connect() as conn:
        await conn.exec_driver_sql("SET enable_seqscan = off")
        rows = await conn.exec_driver_sql(
            "EXPLAIN SELECT id FROM ih_identities WHERE search_text ILIKE '%7911000005%'"
        )
        plan = chr(10).join(str(row[0]) for row in rows)
        await conn.exec_driver_sql("SET enable_seqscan = on")
    assert "ix_ih_identities_search" in plan, plan
