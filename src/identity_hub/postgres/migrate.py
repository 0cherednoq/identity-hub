"""Программный запуск миграций Identity Hub: `await migrate(engine)` или `identity-hub migrate`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text

if TYPE_CHECKING:
    from alembic.config import Config
    from sqlalchemy.engine import Connection
    from sqlalchemy.ext.asyncio import AsyncEngine

__all__ = ["MIGRATION_LOCK_NAMESPACE", "SCHEMA_HEAD", "VERSION_TABLE", "alembic_config", "migrate"]

SCRIPT_LOCATION = "identity_hub.postgres:migrations"
VERSION_TABLE = "ih_alembic_version"
# Ревизия, которую ожидает этот код; новая миграция обязана обновить значение (тест сверяет с alembic head).
SCHEMA_HEAD = "0007"
MIGRATION_LOCK_NAMESPACE = "identity_hub:migrate"


def alembic_config(db_schema: str | None = None, connection: Connection | None = None) -> Config:
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", SCRIPT_LOCATION)
    cfg.attributes["db_schema"] = db_schema
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


async def migrate(engine: AsyncEngine, *, db_schema: str | None = None, revision: str = "head") -> None:
    """Применить миграции библиотеки (создаёт схему и pg_trgm при необходимости)."""
    from alembic import command

    async with engine.begin() as conn:
        # одна миграция на схему за раз; xact-блокировка снимается при commit/rollback (IMP-15)
        await conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:name, 0))"),
            {"name": f"{MIGRATION_LOCK_NAMESPACE}:{db_schema or 'public'}"},
        )
        if db_schema is not None:
            await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{db_schema}"'))

        def _upgrade(sync_conn: Connection) -> None:
            command.upgrade(alembic_config(db_schema, sync_conn), revision)

        await conn.run_sync(_upgrade)
