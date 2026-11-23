"""PostgreSQL backfill and query path for exclusive GCRA recovery."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine

from identity_hub import IdentityCreate, IdentityHub, SpaceHandle, Throttle
from identity_hub.postgres.migrate import migrate
from identity_hub.service import Service

pytestmark = pytest.mark.pg


@pytest.fixture
async def schema(db: AsyncEngine) -> AsyncIterator[str]:
    name = f"ih_gcra_{uuid4().hex[:10]}"
    yield name
    async with db.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))


async def test_upgrade_backfills_latest_unclean_end_in_custom_schema(db: AsyncEngine, schema: str) -> None:
    await migrate(db, db_schema=schema, revision="0041")
    space_id, recovered, clean, deleted = uuid4(), uuid4(), uuid4(), uuid4()
    start = datetime(2026, 11, 23, tzinfo=UTC)
    async with db.begin() as conn:
        await conn.execute(
            text(f"""INSERT INTO "{schema}".ih_services (name, title) VALUES ('svc', 'Svc')""")
        )
        await conn.execute(
            text(f"""INSERT INTO "{schema}".ih_spaces (id, slug, title) VALUES (:id, 's', 'S')"""),
            {"id": space_id},
        )
        await conn.execute(
            text(
                f'INSERT INTO "{schema}".ih_identities (id, space_id, service, external_ref, '
                "normalized_ref, lifecycle, standing, auth_state, availability, error_score, "
                "fencing_seq, event_seq) VALUES (:id, :space, 'svc', :ref, :ref, 'ready', 'good', "
                "'no_session', 'available', 0, 0, 0)"
            ),
            [{"id": identity, "space": space_id, "ref": str(identity)} for identity in (recovered, clean)],
        )
        await conn.execute(
            text(
                f'INSERT INTO "{schema}".ih_lease_history (lease_id, space_id, identity_id, token, '
                "fencing, pool, holder, acquired_at, last_heartbeat_at, succeeded, failed, "
                "released_at, end_reason) VALUES (:lease, :space, :id, :token, "
                "1, 'p', 'w', :start, :start, 0, 0, :end, :reason)"
            ),
            [
                {
                    "lease": uuid4(),
                    "space": space_id,
                    "id": identity,
                    "token": uuid4(),
                    "start": start,
                    "end": start + timedelta(seconds=i),
                    "reason": reason,
                }
                for i, (identity, reason) in enumerate(
                    [
                        (recovered, "revoked"),
                        (recovered, "expired"),
                        (recovered, "max_hold"),
                        (recovered, "yielded"),
                        (recovered, "released"),
                        (clean, "released"),
                        (deleted, "lost"),
                    ]
                )
            ],
        )

    await migrate(db, db_schema=schema)
    async with db.connect() as conn:
        rows = await conn.execute(text(f'SELECT identity_id, ended_at FROM "{schema}".ih_throttle_recovery'))
        assert [tuple(row) for row in rows] == [(recovered, start + timedelta(seconds=3))]


async def test_negative_recovery_query_never_reads_history(
    db: AsyncEngine, hub: IdentityHub, space: SpaceHandle
) -> None:
    service = Service("throttle", "Throttle", throttles={"api": Throttle(1, timedelta(seconds=10))})
    hub.register_service(service)
    await hub.start()
    await space.identities.create(
        IdentityCreate(service=service.name, external_ref="throttle", lifecycle="ready"), actor="test"
    )
    pool = space.pools.define("work", service=service)
    lease = await pool.acquire(for_operation="probe")
    sql: list[str] = []

    def listener(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool) -> None:
        sql.append(statement)

    event.listen(db.sync_engine, "before_cursor_execute", listener)
    try:
        assert await lease.throttle("api", wait=False) == timedelta(0)
    finally:
        event.remove(db.sync_engine, "before_cursor_execute", listener)
        await lease.release()
    assert sum("ih_throttle_recovery" in query for query in sql) == 1
    assert sum("ih_throttle_state" in query for query in sql) == 1
    assert all("ih_lease_history" not in query for query in sql)


@pytest.mark.load
async def test_negative_generic_recovery_plan_with_large_history(db: AsyncEngine, space: SpaceHandle) -> None:
    identity = await space.identities.create(
        IdentityCreate(service="fake_tg", external_ref="large-history", lifecycle="ready"), actor="test"
    )
    space_id = (await space.ref()).id
    async with db.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO ih_lease_history (lease_id, identity_id, space_id, token, fencing, pool, "
                "holder, acquired_at, last_heartbeat_at, released_at, end_reason, succeeded, failed) "
                "SELECT gen_random_uuid(), :identity, :space, gen_random_uuid(), n, 'work', 'test', "
                "now() - n * interval '1 second', now(), now(), 'released', 0, 0 "
                "FROM generate_series(1, 1000000) AS n"
            ),
            {"identity": identity.id, "space": space_id},
        )
        await conn.execute(text("ANALYZE ih_lease_history"))
        await conn.execute(text("SET LOCAL plan_cache_mode = force_generic_plan"))
        await conn.execute(
            text(
                "PREPARE throttle_recovery(uuid, uuid) AS SELECT ended_at FROM ih_throttle_recovery "
                "WHERE identity_id = $1 AND space_id = $2"
            )
        )
        plan = (
            await conn.execute(
                text(
                    "EXPLAIN (ANALYZE, BUFFERS, SETTINGS) "
                    f"EXECUTE throttle_recovery('{identity.id}', '{space_id}')"
                )
            )
        ).scalars()
        description = "\n".join(plan)
        await conn.execute(text("DEALLOCATE throttle_recovery"))
    assert "force_generic_plan" in description
    assert "ih_throttle_recovery" in description and "ih_lease_history" not in description
    assert "actual" in description and "rows=0 loops=1" in description
