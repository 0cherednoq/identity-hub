"""Протокол идемпотентности: канонический fingerprint команды (IMP-03, F1.1).

Ключ идемпотентности живёт в `namespace` (пространство + семейство команд). Вместе с ключом
хранится fingerprint команды: повтор с тем же ключом и другой командой — `IdempotencyConflict`,
а не тихий ответ «уже выполнено».

Канонический JSON: ключи отсортированы, UUID и перечисления — строками, время — UTC ISO 8601,
`OMITTED` (поле не передано) отличается от `None`. Секреты в fingerprint не входят никогда:
`SecretStr`, `SecretValue` и `bytes` отвергаются.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Final
from uuid import UUID

from pydantic import SecretBytes, SecretStr

__all__ = [
    "FINGERPRINT_VERSION",
    "LEGACY_FINGERPRINT_VERSION",
    "OMITTED",
    "OPERATION_FAMILY",
    "canonical_json",
    "fingerprint",
    "namespace",
]

FINGERPRINT_VERSION: Final = 1
# записи, созданные до протокола: fingerprint неизвестен, сверяется только вид команды
LEGACY_FINGERPRINT_VERSION: Final = 0


OPERATION_FAMILY: Final = "operation"
"""Семейство команд `lease.operation(kind, key=...)`."""


def namespace(space_id: UUID, family: str) -> str:
    """Область ключа: одно пространство и одно семейство команд."""
    return f"{space_id}:{family}"


class _Omitted:
    __slots__ = ()

    def __repr__(self) -> str:
        return "OMITTED"


OMITTED: Final = _Omitted()
"""Значение «поле не передано»: ключ с ним выпадает из канонического JSON (в отличие от `None`)."""


def _canonical(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise TypeError("NaN и бесконечность не входят в канонический JSON")
        return value
    if isinstance(value, Enum):
        return _canonical(value.value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise TypeError("время без часового пояса не входит в канонический JSON")
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    if (
        isinstance(value, SecretStr | SecretBytes | bytes | bytearray)
        or type(value).__name__ == "SecretValue"
    ):
        raise TypeError("секрет не может входить в fingerprint команды")
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("ключи канонического JSON — строки")
            if item is not OMITTED:
                result[key] = _canonical(item)
        return result
    if isinstance(value, list | tuple):
        return [_canonical(item) for item in value]
    if isinstance(value, set | frozenset):
        return sorted((_canonical(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True))
    raise TypeError(f"тип {type(value).__name__} не входит в канонический JSON")


def canonical_json(value: Any) -> str:
    """Детерминированная строка: одинаковые команды дают одинаковый результат в любом процессе."""
    return json.dumps(
        _canonical(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def fingerprint(operation: str, payload: Mapping[str, Any]) -> str:
    """SHA-256 канонической команды (версия `FINGERPRINT_VERSION`), hex."""
    body = canonical_json({"v": FINGERPRINT_VERSION, "operation": operation, "payload": payload})
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
