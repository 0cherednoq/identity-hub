"""I6.2 (`R2-D08`): страница админки читает детали пачкой — число запросов не растёт со строками."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

from identity_hub import IdentityCreate, IdentityHub, Lifecycle
from identity_hub.hub import SpaceHandle
from identity_hub.read.search import AccountSearchOptions

pytestmark = pytest.mark.pg


async def _seed(space: SpaceHandle, count: int) -> None:
    await space.identities.create_many(
        [
            IdentityCreate(
                service="fake_tg",
                external_ref=f"+7900000{index:04d}",
                session={"auth_key": "k"},
                lifecycle=Lifecycle.READY,
            )
            for index in range(count)
        ],
        actor="test",
    )


async def test_identity_page_reads_details_in_batches(
    hub: IdentityHub, db: AsyncEngine, space: SpaceHandle
) -> None:
    await _seed(space, 40)
    statements: list[str] = []

    def capture(_conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        statements.append(statement)

    await space.admin.identity_page(limit=2, count=False)  # прогрев
    event.listen(db.sync_engine, "before_cursor_execute", capture)
    try:
        small = await space.admin.identity_page(limit=2, count=False)
        few = len(statements)
        large = await space.admin.identity_page(limit=40, count=False)
        many = len(statements) - few
    finally:
        event.remove(db.sync_engine, "before_cursor_execute", capture)

    assert len(small.items) == 2 and len(large.items) == 40
    assert many == few, f"страница на 40 строк: {many} запросов против {few} на 2\n" + "\n".join(
        statements[few:]
    )


async def test_identity_page_with_quota_filter_stays_batched(
    hub: IdentityHub, db: AsyncEngine, space: SpaceHandle
) -> None:
    await _seed(space, 20)
    statements: list[str] = []

    def capture(_conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        statements.append(statement)

    options = AccountSearchOptions(quota_kind="read_channel")
    await space.admin.identity_page(options=options, limit=2, count=False)
    event.listen(db.sync_engine, "before_cursor_execute", capture)
    try:
        await space.admin.identity_page(options=options, limit=2, count=False)
        few = len(statements)
        page = await space.admin.identity_page(options=options, limit=20, count=False)
        many = len(statements) - few
    finally:
        event.remove(db.sync_engine, "before_cursor_execute", capture)

    assert len(page.items) == 20 and all(item.quotas for item in page.items)
    assert many == few
