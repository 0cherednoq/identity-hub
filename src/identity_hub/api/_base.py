"""Базовый класс неймспейса пространства."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from identity_hub.hub import SpaceHandle

__all__ = ["Namespace"]


class Namespace:
    """Фасад одной группы операций пространства; состояния, кроме ссылки на пространство, нет."""

    __slots__ = ("_space",)

    def __init__(self, space: SpaceHandle) -> None:
        self._space = space

    def __repr__(self) -> str:
        return f"{type(self).__name__}(space={self._space.slug!r})"
