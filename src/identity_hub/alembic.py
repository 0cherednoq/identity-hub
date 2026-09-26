"""Интеграция с Alembic приложения: исключить объекты Identity Hub из autogenerate (ТЗ §8.2).

    from identity_hub.alembic import exclude_identity_hub

    context.configure(..., include_object=exclude_identity_hub)

Или в составе своего фильтра: `exclude_identity_hub(obj, name, type_, reflected, compare_to) and ...`.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Table

__all__ = ["IDENTITY_HUB_PREFIX", "exclude_identity_hub"]

IDENTITY_HUB_PREFIX = "ih_"


def _belongs_to_hub(obj: Any, name: str | None) -> bool:
    if name and name.startswith(IDENTITY_HUB_PREFIX):
        return True
    table = obj if isinstance(obj, Table) else getattr(obj, "table", None)
    return isinstance(table, Table) and table.name.startswith(IDENTITY_HUB_PREFIX)


def exclude_identity_hub(obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any) -> bool:
    """include_object для Alembic: False для таблиц, индексов и ограничений библиотеки."""
    return not _belongs_to_hub(obj, name)
