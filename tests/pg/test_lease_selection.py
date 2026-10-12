"""IMP-01 (I2.1): LRU-выборка аренды по индексу, без перебора занятых (D-209)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

import pytest
from benchmarks.seed import seed_identities
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine

from identity_hub.hub import IdentityHub
from identity_hub.service import OperationRule, Quota, Service

pytestmark = [pytest.mark.pg]

PICK_MARKER = "SKIP LOCKED"


@contextmanager
def _captured(engine: AsyncEngine) -> Iterator[list[tuple[str, Any]]]:
    """SQL выборки кандидата (`FOR UPDATE ... SKIP LOCKED`) с параметрами."""
    seen: list[tuple[str, Any]] = []

    def listener(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool) -> None:
        if PICK_MARKER in statement and "ih_identities" in statement:
            seen.append((statement, parameters))

    event.listen(engine.sync_engine, "before_cursor_execute", listener)
    try:
        yield seen
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", listener)


async def _space(hub: IdentityHub, db: AsyncEngine, count: int) -> Any:
    service = Service(
        "pick",
        "Pick",
        operations={"work": OperationRule(), "send": OperationRule(quotas=(Quota(5, timedelta(hours=1)),))},
    )
    hub.register_service(service)
    await hub.start()
    space = hub.space("pick", create=True)
    await seed_identities(db, space_id=(await space.ref()).id, service="pick", count=count, prefix="pick")
    async with db.begin() as conn:
        await conn.execute(text("ANALYZE ih_identities"))
    return space


async def test_lru_skips_busy_without_retries(hub: IdentityHub, db: AsyncEngine) -> None:
    space = await _space(hub, db, 200)
    pool = space.pools.define("p", service="pick", holder="w")
    busy = [await pool.acquire() for _ in range(180)]  # 90% занято

    with _captured(db) as picks:
        free = await pool.acquire()

    assert len(picks) == 1  # одна выборка: занятые не перебираются повторами
    assert free.identity_id not in {lease.identity_id for lease in busy}
    for lease in [*busy, free]:
        await lease.release()


async def test_pick_uses_lru_index(hub: IdentityHub, db: AsyncEngine) -> None:
    space = await _space(hub, db, 5000)
    pool = space.pools.define("p", service="pick", holder="w")
    with _captured(db) as picks:
        lease = await pool.acquire()
    await lease.release()
    statement, parameters = picks[0]

    async with db.connect() as conn:
        raw = await conn.get_raw_connection()
        plan = await raw.driver_connection.fetch(  # type: ignore[union-attr]
            "EXPLAIN " + statement, *parameters
        )
    plan_text = "\n".join(row[0] for row in plan)
    assert "ix_ih_identities_pick" in plan_text, plan_text


async def test_require_quota_keeps_lru_index_and_probes_quota_by_index(
    hub: IdentityHub, db: AsyncEngine
) -> None:
    """N1.3: фильтр квот не меняет обход LRU-индекса, а счётчик ищется по индексу, без seq scan."""
    space = await _space(hub, db, 5000)
    ref = await space.ref()
    async with db.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO ih_quota_usage "
                "(identity_id, kind, window_seconds, window_start, space_id, used) "
                "SELECT id, 'send', 3600, "
                "date_bin(interval '3600 seconds', now(), timestamptz '2000-01-03 00:00:00+00'), "
                "space_id, 5 FROM ih_identities WHERE space_id = :space ORDER BY id LIMIT 2500"
            ),
            {"space": ref.id},
        )
        await conn.execute(text("ANALYZE ih_quota_usage"))
    pool = space.pools.define("p", service="pick", holder="w")
    with _captured(db) as picks:
        lease = await pool.acquire(for_operation="send", require_quota=True)
    await lease.release()
    statement, parameters = picks[0]
    assert "ih_quota_usage" in statement

    async with db.connect() as conn:
        raw = await conn.get_raw_connection()
        plan = await raw.driver_connection.fetch(  # type: ignore[union-attr]
            "EXPLAIN " + statement, *parameters
        )
    plan_text = "\n".join(row[0] for row in plan)
    assert "ix_ih_identities_pick" in plan_text, plan_text
    assert "Seq Scan on ih_quota_usage" not in plan_text, plan_text


async def test_pick_without_require_quota_does_not_touch_quota_table(
    hub: IdentityHub, db: AsyncEngine
) -> None:
    """N1.3: по умолчанию SQL выборки прежний — таблица счётчиков в нём не участвует."""
    space = await _space(hub, db, 50)
    pool = space.pools.define("p", service="pick", holder="w")
    with _captured(db) as picks:
        lease = await pool.acquire(for_operation="send")
    await lease.release()

    assert "ih_quota_usage" not in picks[0][0]
