"""Базовый класс неймспейса и предупреждение об устаревших методах пространства."""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING

from identity_hub.api._migration import LEGACY

if TYPE_CHECKING:
    from identity_hub.hub import SpaceHandle

__all__ = ["Namespace", "warn_deprecated"]


class Namespace:
    """Фасад одной группы операций пространства; состояния, кроме ссылки на пространство, нет."""

    __slots__ = ("_space",)

    def __init__(self, space: SpaceHandle) -> None:
        self._space = space

    def __repr__(self) -> str:
        return f"{type(self).__name__}(space={self._space.slug!r})"


def warn_deprecated(old: str, *, stacklevel: int = 3) -> None:
    """Предупредить о вызове устаревшего `space.<old>`; замена берётся из `LEGACY`.

    `stacklevel=3` указывает на код пользователя, вызвавший алиас, из которого вызвана функция.
    """
    new = LEGACY[old]
    warnings.warn(
        f"space.{old} устарел и будет удалён в 1.0.0b1, используйте space.{new}",
        DeprecationWarning,
        stacklevel=stacklevel,
    )
