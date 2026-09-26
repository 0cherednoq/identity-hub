"""Точка входа: IdentityHub и пространства (ТЗ §5.1, §5.5, §11)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from identity_hub._space import SpaceBase
from identity_hub.auth import LoginMixin
from identity_hub.clock import Clock, SystemClock
from identity_hub.crypto import Envelope
from identity_hub.dispatch.dispatcher import Dispatcher
from identity_hub.dispatch.notifications import NotificationDispatcher, TelegramTransport
from identity_hub.dispatch.webhooks import WebhookDispatcher, WebhookPolicy
from identity_hub.doctor import Doctor
from identity_hub.errors import NotFound, ValidationFailed
from identity_hub.events import EventDraft
from identity_hub.identities import IdentitiesMixin
from identity_hub.identity_data import IdentityDataMixin
from identity_hub.importing import ImportingMixin
from identity_hub.links import LinksMixin
from identity_hub.logging import LeaseLogPolicy, LeaseLogSink
from identity_hub.maintenance import Maintenance, MaintenanceSchedule
from identity_hub.metrics import MetricsHook, MetricsReporter, NullMetrics
from identity_hub.pool import PoolsMixin
from identity_hub.protocols import SecretsProvider
from identity_hub.proxies import ProxiesMixin, ProxyHealthPolicy
from identity_hub.read.cards import CardsMixin
from identity_hub.read.search import SearchMixin
from identity_hub.secrets_admin import SecretsAdminMixin
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
        lease_logs: LeaseLogPolicy | None = None,
        webhooks: WebhookPolicy | None = None,
        webhook_dev_hosts: Iterable[str] = (),
        telegram_transport: TelegramTransport | None = None,
        maintenance: MaintenanceSchedule | None = None,
        metrics: MetricsHook | None = None,
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
        # sink ih_lease_logs (FR-12.2): подключить `logging.getLogger().addHandler(hub.lease_log_sink)`
        self.lease_log_sink = LeaseLogSink(lease_logs)
        self.dispatcher = Dispatcher(self)
        self.webhook_dev_hosts = frozenset(webhook_dev_hosts)
        self.webhook_dispatcher = WebhookDispatcher(self, webhooks)
        self.dispatcher.add_consumer("webhooks", self.webhook_dispatcher.handle_events)
        self.dispatcher.add_pump(self.webhook_dispatcher.deliver_due)
        if telegram_transport is None:
            from identity_hub.telegram_notify import AiogramTelegramTransport

            telegram_transport = AiogramTelegramTransport()
        self.notification_dispatcher = NotificationDispatcher(
            self, telegram_transport, policy=self.webhook_dispatcher.policy
        )
        self.dispatcher.add_consumer("notifications", self.notification_dispatcher.handle_events)
        self.dispatcher.add_pump(self.notification_dispatcher.deliver_due)
        self.maintenance = Maintenance(self, maintenance)
        self.dispatcher.add_pump(self.maintenance.run_due)
        self.doctor = Doctor(self)
        self.metrics: MetricsHook = metrics or NullMetrics()
        self.metrics_reporter = MetricsReporter(self)
        self.dispatcher.add_pump(self.metrics_reporter.run_due)
        self.events = self.dispatcher  # hub.events.subscribe(name, fn, types=...) — ТЗ §11
        self.strict_events = strict_events  # SecretLeak вместо вырезания секретов в событиях
        self._services: dict[str, Service] = {}
        for service in services:
            self.register_service(service)
        self._spaces: dict[str, SpaceRef] = {}
        self._lease_losses: Counter[tuple[str, str]] = Counter()

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

    # --------------------------------------------------------- агрегаты событий

    def _record_lease_loss(self, space_slug: str, holder: str) -> None:
        self._lease_losses[(space_slug, holder)] += 1

    async def flush_lease_losses(self) -> int:
        """`worker.leases_lost`: одно событие на воркера за период (D-119). Возвращает число событий."""
        losses, self._lease_losses = self._lease_losses, Counter()
        for (slug, holder), count in losses.items():
            space = self.space(slug)
            async with space._tx(own=True) as tx:
                await space._emit(
                    tx,
                    EventDraft("worker.leases_lost", actor=holder, data={"holder": holder, "count": count}),
                )
        return len(losses)

    async def _space_ref(self, slug: str, title: str | None, space_id: UUID | None) -> SpaceRef:
        ref = self._spaces.get(slug)
        if ref is None:
            ref = await self.store.ensure_space(slug, title, space_id)
            self._spaces[slug] = ref
        return ref


class SpaceHandle(
    CardsMixin,
    IdentitiesMixin,
    IdentityDataMixin,
    ImportingMixin,
    LinksMixin,
    LoginMixin,
    PoolsMixin,
    ProxiesMixin,
    RequirementsMixin,
    SearchMixin,
    SecretsAdminMixin,
    StateMixin,
    SyncMixin,
    VerificationsMixin,
):
    """Пространство (ТЗ §5.1). `.using(session)` — работа в общей транзакции пользователя.

    Операции разложены по миксинам: регионы — `state.StateMixin`, далее по задачам плана.
    """
