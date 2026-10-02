"""F4.4b: админ-чтение на объёме — очередь оператора и doctor (R2-D06, R2-D07)."""

from __future__ import annotations

import math
from typing import Any

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine

from identity_hub import IdentityHub, SpaceHandle
from identity_hub.postgres import store as pg_store

pytestmark = pytest.mark.pg


async def _seed(db: AsyncEngine, space: SpaceHandle, count: int, *, requirements: bool) -> None:
    ref = await space.ref()
    value = """'[{"code": "phone_verification"}]'""" if requirements else "'[]'"
    async with db.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO ih_identities (id, space_id, service, external_ref, normalized_ref, lifecycle, "
                "standing, auth_state, availability, error_score, fencing_seq, event_seq, requirements) "
                "SELECT gen_random_uuid(), :space, 'fake_tg', 'ref-' || g, 'ref-' || g, 'ready', 'good', "
                f"'authenticated', 'available', 0, 0, 0, {value}::jsonb FROM generate_series(1, :n) g"
            ),
            {"space": ref.id, "n": count},
        )
        await conn.execute(
            text(
                "INSERT INTO ih_region_states (identity_id, region, key, space_id, state, change_source, "
                "version) SELECT id, 'standing', '', space_id, 'good', 'command', 1 FROM ih_identities"
            )
        )


async def test_human_queue_opens_with_40000_accounts_needing_attention(
    hub: IdentityHub, space: SpaceHandle, db: AsyncEngine
) -> None:
    """R2-D06: список id не упирается в предел 32 767 параметров запроса asyncpg."""
    await _seed(db, space, 40_000, requirements=True)

    page = await space.admin.human_queue(limit=20)

    assert len(page.items) == 20
    assert page.counts["requirement"] == 40_000
    assert page.next_cursor is not None


def _statements(db: AsyncEngine) -> tuple[list[str], Any]:
    seen: list[str] = []

    def capture(_conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        seen.append(statement)

    return seen, capture


@pytest.mark.parametrize("count", [25, 45])
async def test_doctor_reads_in_pages_without_per_account_queries(
    hub: IdentityHub, space: SpaceHandle, db: AsyncEngine, monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    """R2-D07: doctor читает аккаунты страницами; число запросов растёт со страницами, а не с аккаунтами."""
    monkeypatch.setattr(pg_store, "DOCTOR_PAGE", 10)
    await _seed(db, space, count, requirements=False)
    async with db.begin() as conn:
        await conn.execute(text("UPDATE ih_identities SET standing = 'banned' WHERE external_ref = 'ref-7'"))
    seen, capture = _statements(db)

    event.listen(db.sync_engine, "before_cursor_execute", capture)
    try:
        issues = await hub.doctor.check(fix=True)
    finally:
        event.remove(db.sync_engine, "before_cursor_execute", capture)

    assert [issue.kind for issue in issues] == ["standing"]
    pages = math.ceil(count / 10)
    selects = [statement for statement in seen if statement.lstrip().upper().startswith("SELECT")]
    assert len(selects) <= 3 * (pages + 1) + 2  # аккаунты, регионы и события на страницу + прокси
    assert await hub.doctor.check() == []  # исправление записано
