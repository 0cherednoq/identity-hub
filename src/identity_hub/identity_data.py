"""Данные пользователя по неймспейсам (ТЗ FR-3.6).

Escape hatch для небольшого состояния домена (`farm.notcoin`). Библиотека хранит JSON,
версию схемы и версию записи для оптимистической блокировки; содержимое не интерпретирует.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from identity_hub._space import SpaceBase
from identity_hub.api._base import legacy
from identity_hub.domain.data import validate_json_payload
from identity_hub.errors import ConcurrentModification, ValidationFailed

__all__ = ["DataRecord", "IdentityDataMixin"]

_NAMESPACE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$")


@dataclass(frozen=True, slots=True)
class DataRecord[M: BaseModel]:
    value: M
    schema_version: int
    version: int  # передаётся в save(expected_version=...) для оптимистической блокировки
    updated_at: datetime


class IdentityDataMixin(SpaceBase):
    async def _load_data[M: BaseModel](
        self, identity_id: UUID, namespace: str, schema: type[M], *, schema_version: int = 1
    ) -> DataRecord[M] | None:
        """Прочитать неймспейс. Другая версия схемы — ValidationFailed (миграция — у пользователя)."""
        _check_namespace(namespace)
        async with self._tx() as tx:
            await self._identity(tx, identity_id)
            row = await tx.load_data(identity_id, namespace)
        if row is None:
            return None
        if row.schema_version != schema_version:
            raise ValidationFailed(
                f"identity_data {namespace}: версия схемы {row.schema_version}, ожидалась {schema_version}"
            )
        try:
            value = schema.model_validate(row.data)
        except PydanticValidationError as exc:
            raise ValidationFailed(
                f"identity_data {namespace} не соответствует схеме", exc.errors()
            ) from None
        return DataRecord(value, row.schema_version, row.version, row.updated_at)

    async def _save_data(
        self,
        identity_id: UUID,
        namespace: str,
        value: BaseModel,
        *,
        expected_version: int | None,
        schema_version: int = 1,
    ) -> int:
        """Записать неймспейс. `expected_version=None` — создать; иначе — версия из `load_data`.

        Возвращает новую версию; параллельное изменение — ConcurrentModification.
        Выполняется в транзакции пользователя, если она передана (ТЗ §7.2).
        """
        _check_namespace(namespace)
        data = value.model_dump(mode="json")
        validate_json_payload(data, f"identity_data.{namespace}")
        async with self._tx() as tx:
            await self._identity(tx, identity_id)
            version = await tx.save_data(identity_id, namespace, data, schema_version, expected_version)
        if version is None:
            raise ConcurrentModification("identity_data", f"{identity_id}/{namespace}", expected_version)
        return version

    load_data = legacy(_load_data, "load_data")
    save_data = legacy(_save_data, "save_data")


def _check_namespace(namespace: str) -> None:
    if len(namespace) > 128 or not _NAMESPACE.match(namespace):
        raise ValidationFailed(f"неймспейс {namespace!r}: формат `farm.notcoin`, до 128 символов")
