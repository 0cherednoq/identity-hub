"""Программный запуск миграций Identity Hub: `await migrate(engine)` или `identity-hub migrate`."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from identity_hub.errors import ValidationFailed

if TYPE_CHECKING:
    from alembic.config import Config
    from sqlalchemy.engine import Connection
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

__all__ = [
    "MIGRATION_LOCK_NAMESPACE",
    "SCHEMA_COMPATIBLE",
    "SCHEMA_HEAD",
    "VERSION_TABLE",
    "alembic_config",
    "migrate",
]

SCRIPT_LOCATION = "identity_hub.postgres:migrations"
VERSION_TABLE = "ih_alembic_version"
# Ревизия, которую ожидает этот код; новая миграция обязана обновить значение (тест сверяет с alembic head).
SCHEMA_HEAD = "0022"
# Ревизии, на которых этот код работает (D-152, D-182): `hub.start()` принимает любую из них.
# Новая ревизия добавляется в множество; прежние остаются, пока код работает и без неё
# (ревизия только добавляет то, что код использует необязательно: индексы, пересчёт данных,
# колонку со значением по умолчанию). Если код без новой ревизии работать не может, все более
# старые ревизии из множества убираются. Сейчас нижняя граница — 0019: код пишет и читает
# `revision` identity и событий (0017, D-205), ключ отпечатка прокси (0018, D-207) и ключа
# идемпотентности (0019, D-208), время последней аренды (0020, D-209) и несколько credentials
# (0022, D-214). 0021 только удаляет индексы и меняет fillfactor.
SCHEMA_COMPATIBLE = frozenset({"0022"})
MIGRATION_LOCK_NAMESPACE = "identity_hub:migrate"
# имя схемы подставляется в SQL: только обычный идентификатор PostgreSQL (до 63 символов)
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")
_LOCK_KEY = "hashtextextended(:name, 0)"


def alembic_config(db_schema: str | None = None, connection: Connection | None = None) -> Config:
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", SCRIPT_LOCATION)
    cfg.attributes["db_schema"] = db_schema
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


async def migrate(engine: AsyncEngine, *, db_schema: str | None = None, revision: str = "head") -> None:
    """Применить миграции библиотеки до `revision`.

    Права. Схема `db_schema` создаётся, только если её нет: когда схему заранее создал DBA,
    роли достаточно быть её владельцем, право `CREATE` на базу не нужно. Расширение `pg_trgm`
    создаётся, только если его нет (нужно право `CREATE` на базу или установка DBA). Если
    расширение стоит в схеме вне `search_path` (например, `extensions`), эта схема на время
    миграции добавляется в конец пути поиска соединения.

    Транзакции. Каждая ревизия применяется и фиксируется отдельно: сбой ревизии откатывает
    только её, повторный запуск продолжит с неё. Одновременные запуски для одной схемы
    сериализует advisory-блокировка на сессию, поэтому подключение должно идти к PostgreSQL
    напрямую или через пул в режиме сессии — не через PgBouncer в режиме транзакций.

    Правило для новых ревизий. Обычная ревизия — транзакционная и короткая. Индекс на большой
    таблице строится отдельной ревизией, в которой нет ничего другого, вне транзакции и
    идемпотентно — прерванная ревизия должна пройти при повторе:

        def upgrade() -> None:
            with op.get_context().autocommit_block():
                op.create_index(
                    "ix_…", "ih_…", ["…"], schema=_schema(),
                    postgresql_concurrently=True, if_not_exists=True,
                )
    """
    from alembic import command

    if db_schema is not None and not _IDENTIFIER.fullmatch(db_schema):
        raise ValidationFailed(
            "db_schema: нужен идентификатор из латинских букв, цифр и подчёркивания длиной до 63 символов"
        )
    lock = {"name": f"{MIGRATION_LOCK_NAMESPACE}:{db_schema or 'public'}"}

    def _upgrade(sync_conn: Connection) -> None:
        command.upgrade(alembic_config(db_schema, sync_conn), revision)

    async with engine.connect() as conn:
        # одна миграция на схему за раз; блокировка на сессию переживает commit отдельных ревизий
        await conn.execute(text(f"SELECT pg_advisory_lock({_LOCK_KEY})"), lock)
        search_path: str | None = None
        try:
            if db_schema is not None:
                exists = await conn.execute(
                    text("SELECT 1 FROM pg_namespace WHERE nspname = :name"), {"name": db_schema}
                )
                if exists.scalar() is None:
                    await conn.execute(text(f'CREATE SCHEMA "{db_schema}"'))
            search_path = await _expose_trgm(conn)
            await conn.commit()  # дальше транзакциями управляет Alembic: по одной на ревизию
            await conn.run_sync(_upgrade)
            await conn.commit()
        finally:
            try:
                await conn.rollback()
                if search_path is not None:
                    await conn.execute(
                        text("SELECT set_config('search_path', :path, false)"), {"path": search_path}
                    )
                await conn.execute(text(f"SELECT pg_advisory_unlock({_LOCK_KEY})"), lock)
                await conn.commit()
            except SQLAlchemyError:
                # соединение потеряно: блокировку сервер снял сам, в пул его возвращать нельзя
                await conn.invalidate()


async def _expose_trgm(conn: AsyncConnection) -> str | None:
    """Сделать класс операторов `gin_trgm_ops` видимым; вернуть прежний `search_path`, если он менялся."""
    row = (
        await conn.execute(
            text(
                "SELECT n.nspname, n.nspname = ANY (current_schemas(true)) AS visible, "
                "quote_ident(n.nspname) AS quoted, current_setting('search_path') AS path "
                "FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace "
                "WHERE e.extname = 'pg_trgm'"
            )
        )
    ).one_or_none()
    if row is None or row.visible:
        return None  # расширения ещё нет (его создаст ревизия 0001) или оно уже в пути поиска
    path = ", ".join(part for part in (row.path, row.quoted) if part)
    await conn.execute(text("SELECT set_config('search_path', :path, false)"), {"path": path})
    return str(row.path)
