"""F4.2: миграции при ограниченных правах и на непустой базе (R2-D11, R2-D12, R1-20)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import event, make_url, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from identity_hub.errors import ValidationFailed
from identity_hub.postgres.migrate import SCHEMA_HEAD, VERSION_TABLE, migrate

pytestmark = pytest.mark.pg


async def _admin(engine: AsyncEngine, *statements: str) -> None:
    """Команды вне транзакции: CREATE DATABASE и DROP ROLE в ней выполнять нельзя."""
    async with engine.connect() as conn:
        autocommit = await conn.execution_options(isolation_level="AUTOCOMMIT")
        for statement in statements:
            await autocommit.execute(text(statement))


async def _version(engine: AsyncEngine, schema: str) -> str:
    async with engine.connect() as conn:
        return str(
            (await conn.execute(text(f'SELECT version_num FROM "{schema}".{VERSION_TABLE}'))).scalar_one()
        )


@pytest.fixture
async def schema(prepared_db: AsyncEngine) -> AsyncIterator[str]:
    name = f"ih_f42_{uuid4().hex[:10]}"
    yield name
    async with prepared_db.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))


@pytest.mark.parametrize("name", ['x"; DROP SCHEMA public; --', "has space", "1digit", "", "a" * 64])
async def test_schema_name_must_be_an_identifier(prepared_db: AsyncEngine, name: str) -> None:
    """R1-20: имя схемы подставляется в SQL, поэтому принимается только идентификатор."""
    with pytest.raises(ValidationFailed):
        await migrate(prepared_db, db_schema=name)


async def test_schema_created_by_dba_needs_no_create_privilege(
    prepared_db: AsyncEngine, database_url: str, schema: str
) -> None:
    """R2-D11: схему заранее создал DBA — роли приложения право CREATE на базу не нужно."""
    role = f"ih_app_{uuid4().hex[:8]}"
    await _admin(
        prepared_db,
        f"CREATE ROLE {role} LOGIN PASSWORD 'app'",
        f'CREATE SCHEMA "{schema}" AUTHORIZATION {role}',
    )
    app = create_async_engine(make_url(database_url).set(username=role, password="app"))
    try:
        await migrate(app, db_schema=schema)
        assert await _version(app, schema) == SCHEMA_HEAD
    finally:
        await app.dispose()
        await _admin(prepared_db, f'DROP SCHEMA "{schema}" CASCADE', f"DROP ROLE {role}")


@pytest.mark.parametrize("db_schema", [None, "ihs"], ids=["public", "own-schema"])
async def test_pg_trgm_installed_outside_search_path(
    prepared_db: AsyncEngine, database_url: str, db_schema: str | None
) -> None:
    """R2-D11: `pg_trgm` стоит в схеме `extensions`, которой нет в `search_path`."""
    database = f"ih_trgm_{uuid4().hex[:8]}"
    await _admin(prepared_db, f"CREATE DATABASE {database}")
    other = create_async_engine(make_url(database_url).set(database=database))
    try:
        await _admin(other, "CREATE SCHEMA extensions", "CREATE EXTENSION pg_trgm SCHEMA extensions")

        await migrate(other, db_schema=db_schema)

        async with other.connect() as conn:
            operator_schema = (
                await conn.execute(
                    text(
                        "SELECT n.nspname FROM pg_index i "
                        "JOIN pg_class c ON c.oid = i.indexrelid "
                        "JOIN pg_opclass o ON o.oid = i.indclass[0] "
                        "JOIN pg_namespace n ON n.oid = o.opcnamespace "
                        "WHERE c.relname = 'ix_ih_identities_search'"
                    )
                )
            ).scalar_one()
            search_path = (await conn.execute(text("SHOW search_path"))).scalar_one()
        assert operator_schema == "extensions"
        assert "extensions" not in search_path  # путь поиска соединения после миграции прежний
    finally:
        await other.dispose()
        await _admin(prepared_db, f"DROP DATABASE {database} WITH (FORCE)")


async def _seed_identities(
    engine: AsyncEngine, schema: str, refs: list[str], *, normalized: bool = False
) -> tuple[Any, list[Any]]:
    """Аккаунты сырой вставкой; `normalized` — схема уже с колонкой `normalized_ref` (после 0008)."""
    extra = (", normalized_ref", ", :ref") if normalized else ("", "")
    space_id = uuid4()
    ids = [uuid4() for _ in refs]
    async with engine.begin() as conn:
        await conn.execute(text(f"INSERT INTO \"{schema}\".ih_services (name, title) VALUES ('svc', 'Svc')"))
        await conn.execute(
            text(f"INSERT INTO \"{schema}\".ih_spaces (id, slug, title) VALUES (:id, 's', 'S')"),
            {"id": space_id},
        )
        for identity_id, ref in zip(ids, refs, strict=True):
            await conn.execute(
                text(
                    f'INSERT INTO "{schema}".ih_identities '
                    "(id, space_id, service, external_ref, lifecycle, standing, auth_state, availability, "
                    f"error_score, fencing_seq, event_seq{extra[0]}) VALUES (:id, :space, 'svc', :ref, "
                    f"'provisioning', 'good', 'no_session', 'available', 0, 0, 0{extra[1]})"
                ),
                {"id": identity_id, "space": space_id, "ref": ref},
            )
    return space_id, ids


async def test_0008_backfill_is_one_statement_and_matches_str_strip(
    prepared_db: AsyncEngine, schema: str
) -> None:
    """R2-D12: backfill — один `UPDATE`, результат тот же, что у нормализации по умолчанию `str.strip`."""
    tab, newline, nbsp, em_space = chr(9), chr(10), chr(0xA0), chr(0x2003)
    refs = ["  +1 ", f"{tab}user@mail.com{newline}", f"{nbsp}nbsp{em_space}", "inner space", "plain"]
    await migrate(prepared_db, db_schema=schema, revision="0007")
    _space, ids = await _seed_identities(prepared_db, schema, refs)
    updates: list[str] = []

    def capture(_conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        if statement.lstrip().upper().startswith("UPDATE") and "ih_identities" in statement:
            updates.append(statement)

    event.listen(prepared_db.sync_engine, "before_cursor_execute", capture)
    try:
        await migrate(prepared_db, db_schema=schema, revision="0008")
    finally:
        event.remove(prepared_db.sync_engine, "before_cursor_execute", capture)

    async with prepared_db.connect() as conn:
        rows = dict(
            (row.id, row.normalized_ref)
            for row in await conn.execute(text(f'SELECT id, normalized_ref FROM "{schema}".ih_identities'))
        )
    assert rows == {identity_id: ref.strip() for identity_id, ref in zip(ids, refs, strict=True)}
    assert len(updates) == 1


async def test_bound_count_is_recomputed_from_bindings(prepared_db: AsyncEngine, schema: str) -> None:
    """R2-D12: счётчик привязок прокси после upgrade равен числу строк в `ih_bindings`."""
    await migrate(prepared_db, db_schema=schema, revision="0015")
    space_id, identities = await _seed_identities(prepared_db, schema, ["a", "b", "c"], normalized=True)
    busy, idle = uuid4(), uuid4()
    async with prepared_db.begin() as conn:
        for proxy_id, fingerprint, bound_count in ((busy, "f1", 0), (idle, "f2", 5)):
            await conn.execute(
                text(
                    f'INSERT INTO "{schema}".ih_proxies (id, space_id, pool, scheme, host, port, '
                    "fingerprint, status, error_score, fail_streak, status_source, max_identities, "
                    "bound_count, key_id, wrapped_dek, nonce, ciphertext) "
                    "VALUES (:id, :space, 'p', 'socks5', 'h', 1080, :fp, "
                    "'alive', 0, 0, 'command', 3, :bound, 'k1', ''::bytea, ''::bytea, ''::bytea)"
                ),
                {"id": proxy_id, "space": space_id, "fp": fingerprint, "bound": bound_count},
            )
        for identity_id in identities[:2]:
            await conn.execute(
                text(
                    f'INSERT INTO "{schema}".ih_bindings (identity_id, space_id, proxy_id) '
                    "VALUES (:identity, :space, :proxy)"
                ),
                {"identity": identity_id, "space": space_id, "proxy": busy},
            )

    await migrate(prepared_db, db_schema=schema)

    async with prepared_db.connect() as conn:
        counts = dict(
            (row.id, row.bound_count)
            for row in await conn.execute(text(f'SELECT id, bound_count FROM "{schema}".ih_proxies'))
        )
    assert counts == {busy: 2, idle: 0}


async def test_each_migration_commits_separately(prepared_db: AsyncEngine, schema: str) -> None:
    """R2-D12: сбой ревизии не откатывает уже применённые — следующая попытка продолжит с неё."""
    await migrate(prepared_db, db_schema=schema, revision="0005")
    await _seed_identities(prepared_db, schema, ["dup", " dup "])

    with pytest.raises(RuntimeError):
        await migrate(prepared_db, db_schema=schema)  # 0008 остановится на коллизии

    assert await _version(prepared_db, schema) == "0007"
    async with prepared_db.connect() as conn:
        locks = (
            await conn.execute(text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"))
        ).scalar_one()
    assert locks == 0  # блокировка миграции снята и после сбоя
