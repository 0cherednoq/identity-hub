"""PA4.1: keyset/released retention используют индексы и с generic prepared plan."""

from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import event, text

from identity_hub.postgres.migrate import alembic_config, migrate

pytestmark = pytest.mark.pg


async def test_recovery_indexes_upgrade_and_downgrade_preserve_tombstones(engine):
    schema = f"ih_recovery_migration_{uuid4().hex[:10]}"
    names = {
        "ix_ih_proxies_space_id",
        "ix_ih_proxy_allocations_pending",
        "ix_ih_proxy_allocations_released",
        "ix_ih_lease_history_proxy_allocation",
    }
    try:
        await migrate(engine, db_schema=schema, revision="0047")
        async with engine.begin() as conn:
            await conn.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
            await conn.execute(
                text("INSERT INTO ih_spaces (id, slug, title) VALUES (gen_random_uuid(), 's', 'S')")
            )
            await conn.execute(
                text(
                    "INSERT INTO ih_proxy_allocations "
                    "(id, space_id, identity_id, proxy_id, state, created_at, updated_at, ended_at) "
                    "SELECT gen_random_uuid(), id, gen_random_uuid(), gen_random_uuid(), "
                    "'released', now(), now(), now() FROM ih_spaces"
                )
            )
        await migrate(engine, db_schema=schema, revision="0048")
        async with engine.begin() as conn:
            indexes = set(
                (
                    await conn.execute(
                        text("SELECT indexname FROM pg_indexes WHERE schemaname = :schema"),
                        {"schema": schema},
                    )
                ).scalars()
            )
            assert names <= indexes
            await conn.run_sync(lambda sync: command.downgrade(alembic_config(schema, sync), "0047"))
            assert (
                await conn.execute(text(f'SELECT count(*) FROM "{schema}".ih_proxy_allocations'))
            ).scalar_one() == 1
            indexes = set(
                (
                    await conn.execute(
                        text("SELECT indexname FROM pg_indexes WHERE schemaname = :schema"),
                        {"schema": schema},
                    )
                ).scalars()
            )
            assert names.isdisjoint(indexes)
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


async def test_recovery_and_retention_have_indexed_generic_plans(space, hub, db):
    ref = await space.ref()
    async with db.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO ih_proxy_allocations "
                "(id, space_id, identity_id, proxy_id, state, created_at, updated_at, ended_at) "
                "SELECT gen_random_uuid(), :space, gen_random_uuid(), gen_random_uuid(), "
                "'released', now(), now(), now() FROM generate_series(1, 5000)"
            ),
            {"space": ref.id},
        )
        await conn.execute(
            text(
                "INSERT INTO ih_proxies (id, space_id, pool, scheme, host, port, fingerprint, "
                "status, error_score, fail_streak, status_source, max_identities, bound_count, "
                "key_id, wrapped_dek, nonce, ciphertext) SELECT gen_random_uuid(), :space, "
                "'legacy', 'http', '127.0.0.1', 8080, 'recovery-' || g, 'alive', 0, 0, 'command', 1, 0, "
                "'k1', '', '', '' "
                "FROM generate_series(1, 5000) g"
            ),
            {"space": ref.id},
        )
        await conn.execute(
            text(
                "INSERT INTO ih_lease_history (lease_id, space_id, identity_id, token, fencing, "
                "pool, holder, "
                "succeeded, failed) SELECT gen_random_uuid(), :space, gen_random_uuid(), gen_random_uuid(), "
                "1, 'legacy', 'worker', 0, 0 FROM generate_series(1, 5000)"
            ),
            {"space": ref.id},
        )
        await conn.execute(text("ANALYZE ih_proxy_allocations, ih_proxies, ih_lease_history"))

    captured = []

    def capture(_conn, _cursor, statement, parameters, *_rest):
        if (
            "SELECT ih_proxy_allocations" in statement
            or "SELECT ih_proxies" in statement
            or "SELECT ih_lease_history" in statement
        ) and ("ORDER BY" in statement):
            captured.append((statement, parameters))
        if "DELETE FROM ih_proxy_allocations" in statement:
            captured.append((statement, parameters))

    event.listen(db.sync_engine, "before_cursor_execute", capture)
    try:
        async with space._tx() as tx:
            assert await tx.proxy_cleanup_page(16) == ([], None)
            proxies, until = await tx.proxy_counter_page(16)
            assert len(proxies) == 16
            assert len((await tx.proxy_counter_page(16, after=proxies[-1][0], until=until))[0]) == 16
            history, history_upper = await tx.proxy_history_page(16)
            assert len(history) == 16
            assert (
                len((await tx.proxy_history_page(16, after=history[-1].lease_id, until=history_upper))[0])
                == 16
            )
            # Непустой диапазон pending проверяется и без подходящих состояний в большой таблице.
            assert (await tx.proxy_cleanup_page(16, after=UUID(int=0), until=UUID(int=(1 << 128) - 1)))[
                0
            ] == []
        counts = await hub._store.cleanup_maintenance(
            {"lease_history": timedelta(days=365), "operation_keys": timedelta(days=365)},
            frozenset({"proxy_allocations"}),
        )
        assert counts == {"proxy_allocations": 0}
    finally:
        event.remove(db.sync_engine, "before_cursor_execute", capture)

    assert len(captured) >= 6
    async with db.begin() as conn:
        await conn.exec_driver_sql("SET LOCAL plan_cache_mode = force_generic_plan")
        for n, (statement, parameters) in enumerate(captured):
            name = f"proxy_recovery_plan_{n}"
            await conn.exec_driver_sql(f"PREPARE {name} AS {statement}")
            args = ", ".join(_literal(value) for value in parameters)
            plan = "\n".join(
                str(row[0]) for row in await conn.exec_driver_sql(f"EXPLAIN EXECUTE {name}({args})")
            )
            try:
                assert "Seq Scan on ih_proxy_allocations" not in plan, plan
                assert "Seq Scan on ih_proxies" not in plan, plan
                if "SELECT ih_proxy_allocations" in statement and "DELETE" not in statement:
                    assert "ix_ih_proxy_allocations_pending" in plan, plan
                if "SELECT ih_proxies" in statement:
                    assert "ix_ih_proxies_space_id" in plan, plan
                if "SELECT ih_lease_history" in statement and "DELETE" not in statement:
                    assert "ix_ih_lease_history_open_space" in plan, plan
                    assert "Seq Scan on ih_lease_history" not in plan, plan
                if "DELETE" in statement:
                    assert "ix_ih_proxy_allocations_released" in plan, plan
            finally:
                await conn.exec_driver_sql(f"DEALLOCATE {name}")


def _literal(value):
    if isinstance(value, UUID):
        return f"'{value}'::uuid"
    if isinstance(value, timedelta):
        return f"'{value.total_seconds()} seconds'::interval"
    if isinstance(value, datetime):
        return f"'{value.isoformat()}'::timestamptz"
    if isinstance(value, int):
        return str(value)
    # Только собственные структурированные параметры; неизвестный тип не включаем в SQL.
    raise AssertionError(type(value))
