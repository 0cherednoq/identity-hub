"""Порт хранилища (D-008).

Ядро работает с хранилищем только через `Store` и единицу работы `StoreTx`. PostgreSQL-реализация —
`identity_hub.postgres.store`, in-memory — `identity_hub.testing` (P10.1). Интерфейс растёт по мере
реализации задач: каждая задача добавляет абстрактные методы и их реализацию в PgStore.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from identity_hub.events import EventDraft

__all__ = ["ServiceRow", "SpaceRef", "Store", "StoreTx"]


@dataclass(frozen=True, slots=True)
class SpaceRef:
    id: UUID
    slug: str
    title: str


@dataclass(frozen=True, slots=True)
class ServiceRow:
    name: str
    title: str


class StoreTx(ABC):
    """Единица работы в рамках одного пространства (своя транзакция или savepoint пользователя)."""

    space: SpaceRef

    @abstractmethod
    async def now(self) -> datetime:
        """Время БД (`now()`), единое для всей транзакции (ТЗ §8.6)."""

    @abstractmethod
    async def emit(self, drafts: Sequence[EventDraft]) -> list[UUID]:
        """Записать события в журнал в этой транзакции (outbox, FR-13.1, G-4.1).

        `data` уже очищены от секретов. Для событий аккаунта инкрементирует `event_seq`
        и проставляет `sequence`. Внешние события будят диспетчер (NOTIFY при коммите).
        """


class Store(ABC):
    @abstractmethod
    def transaction(
        self, space: SpaceRef, *, session: Any | None = None
    ) -> AbstractAsyncContextManager[StoreTx]:
        """Своя транзакция библиотеки, либо savepoint в сессии пользователя (ТЗ T-1, T-3)."""

    @abstractmethod
    async def sync_services(self, services: Sequence[ServiceRow]) -> None:
        """Зеркало реестра сервисов для админки (FR-1.3)."""

    @abstractmethod
    async def ensure_space(
        self, slug: str, title: str | None = None, space_id: UUID | None = None
    ) -> SpaceRef:
        """Получить или создать пространство."""

    @abstractmethod
    async def get_space(self, slug: str) -> SpaceRef | None: ...
