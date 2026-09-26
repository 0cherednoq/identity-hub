"""PostgreSQL-хранилище Identity Hub: ORM-модели, SQL, миграции."""

from __future__ import annotations

from typing import Any

from identity_hub.postgres.models import IdentityHubBase

__all__ = ["IdentityHubBase", "metadata", "schema_options"]

metadata = IdentityHubBase.metadata


def schema_options(db_schema: str | None) -> dict[str, Any]:
    """execution_options для запросов библиотеки при размещении таблиц в отдельной схеме.

    Таблицы объявлены без схемы; отдельная схема задаётся картой трансляции на каждом
    запросе библиотеки (а не на engine), чтобы не затрагивать таблицы приложения (D-013).
    """
    if db_schema is None:
        return {}
    return {"schema_translate_map": {None: db_schema}}
