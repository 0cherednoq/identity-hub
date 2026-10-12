"""Общий результат команд над множеством сущностей (`D-224`).

Пакетная команда выполняет пачку одной транзакцией. По умолчанию отказ одной сущности
откатывает только её и попадает в `rejected`; при `atomic=True` первый отказ отменяет всю
пачку и пробрасывается исходным исключением.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["MAX_BATCH", "BatchRejection", "BatchResult"]

MAX_BATCH = 1000
"""Потолок пачки: больше — `ValidationFailed` без изменений."""


@dataclass(frozen=True, slots=True)
class BatchRejection[K]:
    """Отказ по одной сущности: стабильный `code` ошибки и её безопасный текст."""

    key: K
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class BatchResult[K]:
    """Итог пакетной команды: ключи применённых сущностей и отказы. Порядок — по ключу."""

    applied: list[K] = field(default_factory=list)
    rejected: list[BatchRejection[K]] = field(default_factory=list)
