"""Генерация идентификаторов: UUIDv7 (упорядочен по времени), см. ТЗ §8.4 и D-006."""

from __future__ import annotations

import sys
import threading
from uuid import UUID

if sys.version_info >= (3, 14):
    from uuid import uuid7 as _uuid7
else:  # pragma: no cover - зависит от версии Python
    from uuid_utils.compat import uuid7 as _uuid7

__all__ = ["new_id"]

_COUNTER_BITS = 74  # rand_a (12 бит) и rand_b (62 бита): счётчик внутри миллисекунды
_COUNTER_LIMIT = 1 << _COUNTER_BITS
_RAND_B_MASK = (1 << 62) - 1
_lock = threading.Lock()
_last = 0  # (миллисекунда << 74) | счётчик последнего выданного id


def new_id() -> UUID:
    """Новый UUIDv7, строго больший предыдущего в этом процессе (R2-D24).

    Генератор может вернуть повтор или значение меньше прежнего (несколько id в одной
    миллисекунде, перевод системных часов назад, разные реализации `uuid7`). Тогда берётся
    предыдущее значение плюс единица счётчика; при переполнении счётчика — следующая миллисекунда.
    """
    global _last
    value = _uuid7().int
    candidate = ((value >> 80) << _COUNTER_BITS) | (((value >> 64) & 0xFFF) << 62) | (value & _RAND_B_MASK)
    with _lock:
        if candidate <= _last:
            candidate = _last + 1  # переполнение счётчика переносится в миллисекунды само
        _last = candidate
    millisecond, counter = candidate >> _COUNTER_BITS, candidate & (_COUNTER_LIMIT - 1)
    return UUID(
        int=(millisecond << 80)
        | (7 << 76)
        | ((counter >> 62) << 64)
        | (0b10 << 62)
        | (counter & _RAND_B_MASK)
    )
