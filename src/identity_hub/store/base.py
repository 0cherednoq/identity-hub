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

from identity_hub.crypto.envelope import EncryptedPayload
from identity_hub.domain.regions import RegionState
from identity_hub.events import EventDraft

__all__ = ["IdentityRow", "NewIdentity", "ServiceRow", "SpaceRef", "Store", "StoreTx"]


@dataclass(frozen=True, slots=True)
class SpaceRef:
    id: UUID
    slug: str
    title: str


@dataclass(frozen=True, slots=True)
class ServiceRow:
    name: str
    title: str


@dataclass(frozen=True, slots=True)
class IdentityRow:
    """Строка ih_identities без секретов."""

    id: UUID
    space_id: UUID
    service: str
    external_ref: str
    lifecycle: str
    standing: str
    standing_code: str | None
    standing_until: datetime | None
    auth_state: str
    availability: str
    availability_until: datetime | None
    requirements: list[Any]
    tags: list[str]
    attributes: dict[str, Any]
    profile: dict[str, Any]
    labels: dict[str, Any]
    note: str | None
    search_text: str
    verified_factors: list[str]
    error_score: float
    fencing_seq: int
    event_seq: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class NewIdentity:
    """Вставка аккаунта: данные уже провалидированы, секреты зашифрованы."""

    id: UUID
    service: str
    external_ref: str
    lifecycle: str
    standing: str
    auth_state: str
    tags: list[str]
    attributes: dict[str, Any]
    profile: dict[str, Any]
    labels: dict[str, Any]
    note: str | None
    search_text: str
    profile_secret: EncryptedPayload | None = None


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

    # --------------------------------------------------------- аккаунт и регионы (P2.3)

    @abstractmethod
    async def get_identity(self, identity_id: UUID) -> IdentityRow | None:
        """Аккаунт этого пространства (чужое пространство — None)."""

    @abstractmethod
    async def update_identity(
        self, identity_id: UUID, values: dict[str, Any], expected: dict[str, Any] | None = None
    ) -> bool:
        """Обновить колонки ih_identities; `expected` — compare-and-set (D-107). False — не совпало."""

    @abstractmethod
    async def load_region(self, identity_id: UUID, region: str, key: str) -> RegionState | None: ...

    @abstractmethod
    async def save_region(
        self, identity_id: UUID, region: str, key: str, previous: RegionState | None, new: RegionState
    ) -> bool:
        """Записать регион с проверкой `version` (previous=None — вставка). False — гонка."""

    # --------------------------------------------------------- регистрация (P2.4)

    @abstractmethod
    async def find_identity(
        self, identity_id: UUID, service: str, external_ref: str
    ) -> tuple[UUID, UUID] | None:
        """(id, space_id) аккаунта с этим id или (service, external_ref) — во всех пространствах (FR-2.2)."""

    @abstractmethod
    async def insert_identity(self, row: NewIdentity) -> bool:
        """False — конфликт уникальности (параллельная регистрация)."""

    @abstractmethod
    async def existing_identities(self, ids: Sequence[UUID]) -> set[UUID]:
        """Какие из аккаунтов есть в этом пространстве."""

    @abstractmethod
    async def put_credentials(self, identity_id: UUID, payload: EncryptedPayload) -> None: ...

    @abstractmethod
    async def put_session(
        self,
        identity_id: UUID,
        slot: str,
        payload: EncryptedPayload,
        *,
        fencing: int,
        expires_at: datetime | None = None,
    ) -> bool:
        """Записать сессию слота; отклоняется, если записана более новой арендой (G-1.4)."""

    @abstractmethod
    async def insert_link(self, identity_id: UUID, role: str, linked_identity_id: UUID) -> bool:
        """False — связь с этой ролью уже есть (FR-4.1)."""


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
