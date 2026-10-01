"""E1-E3: миграции в public и отдельной схеме, дрейф, downgrade, права."""

from __future__ import annotations

import traceback

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from identity_hub.postgres import metadata, schema_options
from identity_hub.postgres.migrate import alembic_config, migrate
from sch_common import admin_exec, fresh_db, run, url

OBJECTS = """
select n.nspname, c.relkind, c.relname from pg_class c join pg_namespace n on n.oid=c.relnamespace
where n.nspname not in ('pg_catalog','information_schema','pg_toast') and c.relkind in ('r','i','S','v')
order by 1,2,3
"""


async def drift(engine, schema, server_default):  # type: ignore[no-untyped-def]
    def _diff(sync_conn):  # type: ignore[no-untyped-def]
        conn = sync_conn.execution_options(**schema_options(schema)) if schema else sync_conn
        ctx = MigrationContext.configure(
            conn,
            opts={
                "compare_type": True,
                "compare_server_default": server_default,
                "include_object": lambda o, n, t, r, c: (n or "").startswith("ih_") if t == "table" else True,
                "version_table": "ih_alembic_version",
                "version_table_schema": schema,
                "include_schemas": False,
            },
        )
        return compare_metadata(ctx, metadata)

    async with engine.connect() as conn:
        return await conn.run_sync(_diff)


async def downgrade(engine, schema, rev):  # type: ignore[no-untyped-def]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: command.downgrade(alembic_config(schema, c), rev))


async def main() -> None:
    # ---------- public
    eng = await fresh_db("sch_e1_public")
    await migrate(eng)
    print("public drift (types):", await drift(eng, None, False))
    try:
        print("public drift (server defaults):", await drift(eng, None, True))
    except Exception as exc:
        print("server default compare failed:", type(exc).__name__, exc)
    await downgrade(eng, None, "base")
    async with eng.connect() as conn:
        left = (await conn.execute(text(OBJECTS))).all()
        ext = (await conn.execute(text("select extname, extnamespace::regnamespace::text from pg_extension"))).all()
    print("after downgrade base, objects left:", left, "extensions:", ext)
    await migrate(eng)
    print("re-upgrade ok")
    # пошаговый downgrade/upgrade
    for rev in ["0007", "0006", "0005", "0004", "0003", "0002", "0001"]:
        await downgrade(eng, None, rev)
    await migrate(eng)
    print("stepwise down/up ok; drift:", await drift(eng, None, False))
    await eng.dispose()

    # ---------- отдельная схема
    eng = await fresh_db("sch_e1_schema")
    await migrate(eng, db_schema="ihs")
    async with eng.connect() as conn:
        rows = (await conn.execute(text(OBJECTS))).all()
        outside = [r for r in rows if r[0] != "ihs"]
        ext = (await conn.execute(text("select extname, extnamespace::regnamespace::text from pg_extension"))).all()
        ops = (
            await conn.execute(
                text(
                    "select distinct opcnamespace::regnamespace::text from pg_opclass where opcname='gin_trgm_ops'"
                )
            )
        ).all()
    print("schema: total objects", len(rows), "outside ihs:", outside, "ext:", ext, "trgm opclass ns:", ops)
    print("schema drift:", await drift(eng, "ihs", False))
    await downgrade(eng, "ihs", "base")
    async with eng.connect() as conn:
        print("schema after downgrade:", (await conn.execute(text(OBJECTS))).all())
    await migrate(eng, db_schema="ihs")
    print("schema re-upgrade ok")
    await eng.dispose()

    # ---------- pg_trgm установлен в схему вне search_path (Supabase-подобно: extensions)
    eng = await fresh_db("sch_e1_ext")
    await admin_exec("CREATE SCHEMA extensions", "CREATE EXTENSION pg_trgm SCHEMA extensions", db="sch_e1_ext")
    try:
        await migrate(eng, db_schema="ihs")
        print("ext-in-other-schema: OK")
    except Exception as exc:
        print("ext-in-other-schema: FAIL", type(exc).__name__, str(exc).splitlines()[0][:300])
    await eng.dispose()

    # ---------- роль без CREATE на БД (не владелец), расширение НЕ установлено
    await admin_exec(
        "DROP ROLE IF EXISTS sch_app",
        "CREATE ROLE sch_app LOGIN PASSWORD 'app'",
    )
    eng0 = await fresh_db("sch_e1_role")
    await eng0.dispose()
    await admin_exec("GRANT CREATE ON SCHEMA public TO sch_app", db="sch_e1_role")
    eng = create_async_engine(url("sch_e1_role", "sch_app", "app"))
    try:
        await migrate(eng)
        print("no-CREATE-EXTENSION role, ext missing: OK")
    except Exception as exc:
        print("no-CREATE-EXTENSION role, ext missing: FAIL", type(exc).__name__, str(exc).splitlines()[0][:300])
    # расширение предустановлено DBA
    await admin_exec("CREATE EXTENSION IF NOT EXISTS pg_trgm", db="sch_e1_role")
    try:
        await migrate(eng)
        print("no-CREATE-EXTENSION role, ext preinstalled: OK")
    except Exception as exc:
        print("ext preinstalled: FAIL", type(exc).__name__, str(exc).splitlines()[0][:300])
    # схема создана DBA и отдана роли, права CREATE на БД нет
    await admin_exec("CREATE SCHEMA ihs AUTHORIZATION sch_app", db="sch_e1_role")
    try:
        await migrate(eng, db_schema="ihs")
        print("pre-created schema without CREATE on database: OK")
    except Exception as exc:
        print("pre-created schema: FAIL", type(exc).__name__, str(exc).splitlines()[0][:300])
    # схема не существует, права нет
    try:
        await migrate(eng, db_schema="ihs2")
        print("missing schema without CREATE: OK")
    except Exception as exc:
        print("missing schema without CREATE: FAIL", type(exc).__name__, str(exc).splitlines()[0][:300])
    await eng.dispose()


if __name__ == "__main__":
    try:
        run(main())
    except Exception:
        traceback.print_exc()
