"""`SecretValue` — секрет, который нельзя случайно вывести в лог, событие или JSON (IMP-16)."""

from __future__ import annotations

import hmac
from typing import Any, NoReturn

__all__ = ["SecretValue"]

_MASK = "**********"


class SecretValue:
    """Обёртка секрета: `str`/`repr` скрыты, JSON и pickle запрещены, раскрытие — только `reveal()`.

    Раскрывать значение допустимо в двух местах: под арендой (свой сетевой клиент) и в
    аудируемой команде админки (`space.secrets.reveal`).
    """

    __slots__ = ("_value",)

    def __init__(self, value: str | bytes) -> None:
        if not isinstance(value, str | bytes):
            raise TypeError("SecretValue хранит только str или bytes")
        self._value = value

    def reveal(self) -> str | bytes:
        """Явно получить значение секрета."""
        return self._value

    def __repr__(self) -> str:
        return f"SecretValue('{_MASK}')"

    def __str__(self) -> str:
        return _MASK

    def __format__(self, spec: str) -> str:
        return _MASK

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SecretValue):
            return NotImplemented
        left, right = self._value, other._value
        if isinstance(left, str):
            left = left.encode()
        if isinstance(right, str):
            right = right.encode()
        return hmac.compare_digest(left, right)

    __hash__ = None  # type: ignore[assignment]

    def __bool__(self) -> bool:
        return bool(self._value)

    def __reduce__(self) -> NoReturn:
        raise TypeError("SecretValue нельзя сериализовать pickle")

    def __getstate__(self) -> NoReturn:
        raise TypeError("SecretValue нельзя сериализовать")

    def __copy__(self) -> SecretValue:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> SecretValue:
        return self
