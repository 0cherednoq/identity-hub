"""Окружение Alembic для собственной ветки миграций Identity Hub (ТЗ §8.2).

Своя таблица версий `ih_alembic_version`; отдельная схема Postgres — через
`config.attributes["db_schema"]` (D-013).
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from identity_hub.postgres import metadata, schema_options
from identity_hub.postgres.migrate import VERSION_TABLE

config = context.config


def _configure(connection: Connection) -> None:
    db_schema: str | None = config.attributes.get("db_schema")
    if db_schema is not None:
        connection = connection.execution_options(**schema_options(db_schema))
    context.configure(
        connection=connection,
        target_metadata=metadata,
        version_table=VERSION_TABLE,
        version_table_schema=db_schema,
        compare_type=True,
        compare_server_default=False,
        include_object=_only_identity_hub,
        render_item=_render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def _only_identity_hub(obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any) -> bool:
    if type_ == "table":
        return bool(name and name.startswith("ih_"))
    return True


def _render_item(type_: str, obj: Any, autogen_context: Any) -> str | Literal[False]:
    # XID8 импортируется шаблоном script.py.mako
    if type_ == "type" and type(obj).__name__ == "XID8":
        return "XID8()"
    return False


async def _run_async(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            await conn.run_sync(_configure)
            await conn.commit()
    finally:
        await engine.dispose()


def run() -> None:
    connection: Connection | None = config.attributes.get("connection")
    if connection is not None:
        _configure(connection)
        return
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError("не задан sqlalchemy.url или config.attributes['connection']")
    asyncio.run(_run_async(url))


if context.is_offline_mode():
    raise RuntimeError("offline-режим не поддерживается: используйте identity-hub migrate")
run()
