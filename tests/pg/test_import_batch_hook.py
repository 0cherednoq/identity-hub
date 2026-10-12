"""C5.1: хук импорта внутри транзакции пачки — строки приложения атомарны с аккаунтами."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Column, MetaData, Table, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.types import Uuid

from identity_hub import ImportRecord
from identity_hub.hub import SpaceHandle
from identity_hub.postgres.models import EventRecord, IdentityRecord

pytestmark = pytest.mark.pg


@pytest.fixture
async def accounts(db: AsyncEngine) -> AsyncIterator[Table]:
    """Таблица приложения со ссылкой на аккаунт библиотеки."""
    table = Table(
        f"console_accounts_{uuid4().hex[:8]}", MetaData(), Column("identity_id", Uuid, primary_key=True)
    )
    async with db.begin() as conn:
        await conn.run_sync(table.metadata.create_all)
    yield table
    async with db.begin() as conn:
        await conn.run_sync(table.metadata.drop_all)


async def _count(sessionmaker: async_sessionmaker[AsyncSession], source: object) -> int:
    async with sessionmaker() as session:
        return int((await session.execute(select(func.count()).select_from(source))).scalar_one())  # type: ignore[arg-type]


def _records(count: int, prefix: str = "+") -> list[ImportRecord]:
    return [ImportRecord(external_ref=f"{prefix}{n}") for n in range(count)]


async def test_hook_writes_in_batch_transaction(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession], accounts: Table
) -> None:
    """C5.1: хук видит созданные аккаунты и пишет свои строки той же фиксацией."""
    batches: list[list[UUID]] = []

    async def on_batch(session: AsyncSession, created: list[UUID]) -> None:
        visible = (
            await session.execute(
                select(func.count()).select_from(IdentityRecord).where(IdentityRecord.id.in_(created))
            )
        ).scalar_one()
        assert visible == len(created)  # незафиксированные аккаунты видны в транзакции пачки
        await session.execute(accounts.insert(), [{"identity_id": identity_id} for identity_id in created])
        batches.append(created)

    report = await space.imports.run(
        service="fake_tg", records=_records(5), batch_size=2, on_batch=on_batch, actor="test"
    )

    assert [len(batch) for batch in batches] == [2, 2, 1]
    assert sorted(report.created) == sorted(i for batch in batches for i in batch)
    assert report.failed == [] and report.hook_failed == []
    assert await _count(sessionmaker, accounts) == 5

    again = await space.imports.run(service="fake_tg", records=_records(5), on_batch=on_batch, actor="test")
    assert again.created == [] and len(again.skipped) == 5 and len(batches) == 3  # пачка без новых — без хука


async def test_failed_hook_rolls_back_its_batch(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession], accounts: Table
) -> None:
    """C5.1: сбой хука откатывает пачку: нет ни аккаунтов, ни строк приложения, ни событий."""
    events = await _count(sessionmaker, EventRecord)
    calls = 0

    async def on_batch(session: AsyncSession, created: list[UUID]) -> None:
        nonlocal calls
        calls += 1
        await session.execute(accounts.insert(), [{"identity_id": identity_id} for identity_id in created])
        if calls == 2:
            raise RuntimeError("secret-text в ошибке приложения")

    report = await space.imports.run(
        service="fake_tg", records=_records(6), batch_size=2, on_batch=on_batch, actor="test"
    )

    assert len(report.created) == 4 and report.hook_failed == []
    assert sorted(ref for ref, _reason in report.failed) == ["+2", "+3"]
    assert all("secret-text" not in reason and "RuntimeError" in reason for _ref, reason in report.failed)
    assert await _count(sessionmaker, accounts) == 4
    assert await _count(sessionmaker, IdentityRecord) == 4
    assert await space.identities.find(service="fake_tg", external_ref="+2") is None
    assert await _count(sessionmaker, EventRecord) > events

    retry = await space.imports.run(service="fake_tg", records=_records(6), on_batch=on_batch, actor="test")
    assert (
        len(retry.created) == 2 and await _count(sessionmaker, accounts) == 6
    )  # повтор доводит импорт до конца


async def test_both_hooks_work_together(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession], accounts: Table
) -> None:
    """C5.1: `on_batch` — внутри транзакции, `on_batch_committed` — после фиксации, как раньше."""
    order: list[str] = []

    async def on_batch(session: AsyncSession, created: list[UUID]) -> None:
        order.append("in")

    async def committed(session: AsyncSession, created: list[UUID]) -> None:
        assert await _count(sessionmaker, IdentityRecord) == len(created)  # пачка уже зафиксирована
        order.append("after")

    await space.imports.run(
        service="fake_tg", records=_records(2), on_batch=on_batch, on_batch_committed=committed, actor="test"
    )

    assert order == ["in", "after"]
