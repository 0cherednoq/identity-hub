"""Точка входа: IdentityHub и пространства (ТЗ §5.1, §5.5, §11)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from identity_hub._space import SpaceBase
from identity_hub.auth import LoginMixin
from identity_hub.clock import Clock, SystemClock
from identity_hub.crypto import Envelope
from identity_hub.errors import NotFound, ValidationFailed
from identity_hub.identities import IdentitiesMixin
from identity_hub.identity_data import IdentityDataMixin
from identity_hub.links import LinksMixin
from identity_hub.pool import PoolsMixin
from identity_hub.protocols import SecretsProvider
from identity_hub.proxies import ProxiesMixin, ProxyHealthPolicy
from identity_hub.service import Service
from identity_hub.state import StateMixin
from identity_hub.store.base import ServiceRow, SpaceRef, Store
from identity_hub.sync import SyncMixin
from identity_hub.verifications import RequirementsMixin, VerificationsMixin

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = ["IdentityHub", "Retention", "SpaceBase", "SpaceHandle"]


@dataclass(frozen=True)
class Retention:
    """Сроки хранения журналов (ТЗ §8.9, D-004)."""

    events: timedelta = timedelta(days=180)
    lease_history: timedelta = timedelta(days=90)
    lease_logs: timedelta = timedelta(days=30)
    deliveries: timedelta = timedelta(days=30)
    operation_keys: timedelta = timedelta(days=7)


class IdentityHub:
    """Один на приложение (Dishka `Scope.APP`): хранилище, шифрование, реестр сервисов."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        secrets: SecretsProvider | None = None,
        services: Iterable[Service] = (),
        *,
        admin_base_url: str | None = None,
        retention: Retention | None = None,
        db_schema: str | None = None,
        clock: Clock | None = None,
        strict_events: bool = False,
        store: Store | None = None,
        proxy_health: ProxyHealthPolicy | None = None,
    ) -> None:
        if store is None:
            if sessionmaker is None:
                raise ValidationFailed("нужен sessionmaker (PostgreSQL) или store")
            from identity_hub.postgres.store import PgStore

            store = PgStore(sessionmaker, db_schema=db_schema)
        if secrets is None:
            raise ValidationFailed("нужен SecretsProvider (мастер-ключи)")
        self.store = store
        self.envelope = Envelope(secrets)
        self.admin_base_url = admin_base_url.rstrip("/") if admin_base_url else None
        self.retention = retention or Retention()
        self.clock: Clock = clock or SystemClock()
        self.proxy_health = proxy_health or ProxyHealthPolicy()
        self.strict_events = strict_events  # SecretLeak вместо вырезания секретов в событиях
        self._services: dict[str, Service] = {}
        for service in services:
            self.register_service(service)
        self._spaces: dict[str, SpaceRef] = {}

    # --------------------------------------------------------- сервисы

    def register_service(self, service: Service) -> None:
        if service.name in self._services and self._services[service.name] is not service:
            raise ValidationFailed(f"сервис {service.name} уже зарегистрирован")
        self._services[service.name] = service

    def service(self, name: str | Service) -> Service:
        key = name.name if isinstance(name, Service) else name
        try:
            return self._services[key]
        except KeyError:
            raise NotFound("service", key) from None

    @property
    def services(self) -> tuple[Service, ...]:
        return tuple(self._services.values())

    async def start(self) -> None:
        """Синхронизировать реестр сервисов в ih_services (FR-1.3). Вызывать при старте приложения."""
        await self.store.sync_services([ServiceRow(s.name, s.title) for s in self._services.values()])

    # --------------------------------------------------------- пространства

    def space(self, slug: str, title: str | None = None, *, space_id: UUID | None = None) -> SpaceHandle:
        """Пространство создаётся при первом обращении к БД."""
        return SpaceHandle(self, slug, title=title, space_id=space_id)

    async def _space_ref(self, slug: str, title: str | None, space_id: UUID | None) -> SpaceRef:
        ref = self._spaces.get(slug)
        if ref is None:
            ref = await self.store.ensure_space(slug, title, space_id)
            self._spaces[slug] = ref
        return ref


class SpaceHandle(
    IdentitiesMixin,
    IdentityDataMixin,
    LinksMixin,
    LoginMixin,
    PoolsMixin,
    ProxiesMixin,
    RequirementsMixin,
    StateMixin,
    SyncMixin,
    VerificationsMixin,
):
    """Пространство (ТЗ §5.1). `.using(session)` — работа в общей транзакции пользователя.

    Операции разложены по миксинам: регионы — `state.StateMixin`, далее по задачам плана.
    """
