"""I6.3 (`PERF-1`): пачка импорта пишется серией запросов, не запросом на запись."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

from identity_hub import IdentityHub, ImportRecord
from identity_hub.hub import SpaceHandle

pytestmark = pytest.mark.pg


def _records(prefix: str, count: int) -> list[ImportRecord]:
    return [
        ImportRecord(
            f"{prefix}{index:05d}",
            credentials={"phone": f"{prefix}{index:05d}", "password": "p"},
            sessions={"default": {"auth_key": "k"}},
        )
        for index in range(count)
    ]


async def test_batch_write_does_not_scale_with_rows(
    hub: IdentityHub, db: AsyncEngine, space: SpaceHandle
) -> None:
    statements: list[str] = []

    def capture(_conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        statements.append(statement)

    await space.imports.run(service="fake_tg", records=_records("+7930", 2), actor="test")  # прогрев
    event.listen(db.sync_engine, "before_cursor_execute", capture)
    try:
        await space.imports.run(service="fake_tg", records=_records("+7931", 2), actor="test")
        few = len(statements)
        report = await space.imports.run(
            service="fake_tg", records=_records("+7932", 40), batch_size=100, actor="test"
        )
        many = len(statements) - few
    finally:
        event.remove(db.sync_engine, "before_cursor_execute", capture)

    assert len(report.created) == 40
    # допускается небольшой рост (строковые параметры), но не кратный числу записей
    assert many <= few + 4, f"пачка 40 записей: {many} запросов против {few} на 2\n" + "\n".join(
        statements[few:]
    )


async def test_batch_rollback_leaves_no_rows(hub: IdentityHub, db: AsyncEngine, space: SpaceHandle) -> None:
    """Сбой хука откатывает всю пачку: ни аккаунтов, ни дочерних строк (`DoD I6.3`)."""

    async def broken(session: Any, created: list[Any]) -> None:
        raise RuntimeError("application failure")

    report = await space.imports.run(
        service="fake_tg", records=_records("+7933", 5), actor="test", on_batch=broken
    )

    assert not report.created and len(report.failed) == 5
    page = await space.identities.search("+7933", limit=50, count=False)
    assert not page.items
