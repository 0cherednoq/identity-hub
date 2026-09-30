"""`space.data` — версионируемые данные приложения по неймспейсам identity (IMP-23).

Данные хранятся открытым JSON: прогресс прогрева, курсоры, счётчики. Не для секретов.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel

from identity_hub.api._base import Namespace
from identity_hub.identity_data import DataRecord

__all__ = ["IdentityData"]


class IdentityData(Namespace):
    """Данные приложения: `space.data`."""

    __slots__ = ()

    async def load[M: BaseModel](
        self, identity_id: UUID, namespace: str, schema: type[M], *, schema_version: int = 1
    ) -> DataRecord[M] | None:
        """Прочитать неймспейс (`farm.warmup`); `None` — записи нет.

        Другая `schema_version` — `ValidationFailed`: миграция данных на стороне приложения.
        """
        return await self._space._load_data(identity_id, namespace, schema, schema_version=schema_version)

    async def save(
        self,
        identity_id: UUID,
        namespace: str,
        value: BaseModel,
        *,
        expected_version: int | None,
        schema_version: int = 1,
    ) -> int:
        """Записать неймспейс и вернуть новую версию.

        `expected_version=None` — создать; иначе версия из `load`. Устаревшая версия —
        `ConcurrentModification`.
        """
        return await self._space._save_data(
            identity_id, namespace, value, expected_version=expected_version, schema_version=schema_version
        )
