"""Генерация идентификаторов: UUIDv7 (упорядочен по времени), см. ТЗ §8.4 и D-006."""

from __future__ import annotations

import sys
from uuid import UUID

if sys.version_info >= (3, 14):
    from uuid import uuid7 as _uuid7
else:  # pragma: no cover - зависит от версии Python
    from uuid_utils.compat import uuid7 as _uuid7

__all__ = ["new_id"]


def new_id() -> UUID:
    """Новый UUIDv7."""
    value: UUID = _uuid7()
    return value
