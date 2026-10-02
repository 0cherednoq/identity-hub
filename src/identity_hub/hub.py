"""Точка входа: IdentityHub и пространства (ТЗ §5.1, §5.5, §11)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import timedelta
from functools import cached_property
from typing import TYPE_CHECKING
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from identity_hub._space import SpaceBase
from identity_hub.admin_audit import AdminAuditMixin
from identity_hub.api.admin import Admin
from identity_hub.api.analytics import Analytics
from identity_hub.api.auth import Auth
from identity_hub.api.data import IdentityData
from identity_hub.api.identities import Identities
from identity_hub.api.imports import Imports
from identity_hub.api.links import IdentityLinks
from identity_hub.api.maintenance import SpaceMaintenance
from identity_hub.api.pools import Pools
from identity_hub.api.proxies import Proxies
from identity_hub.api.secrets import Secrets
from identity_hub.api.sync import Sync
from identity_hub.api.verifications import Verifications
from identity_hub.auth import LoginMixin
from identity_hub.clock import Clock, SystemClock
from identity_hub.crypto import Envelope
from identity_hub.dispatch.dispatcher import Dispatcher
from identity_hub.dispatch.notifications import NotificationDispatcher, TelegramTransport
from identity_hub.dispatch.webhooks import WebhookDispatcher, WebhookPolicy
from identity_hub.doctor import Doctor
from identity_hub.errors import NotFound, SchemaIncompatible, ValidationFailed
from identity_hub.events import EventDraft
from identity_hub.identities import IdentitiesMixin
from identity_hub.identity_data import IdentityDataMixin
from identity_hub.importing import ImportingMixin, ImportRunner
from identity_hub.links import LinksMixin
from identity_hub.logging import LeaseLogPolicy, LeaseLogSink
from identity_hub.maintenance import Maintenance, MaintenanceSchedule
from identity_hub.metrics import (
    CompositeMetrics,
    MetricsHook,
    MetricsReporter,
    NullMetrics,
    RollupMetricsBuffer,
)
from identity_hub.pool import PoolsMixin
from identity_hub.protocols import SecretsProvider
from identity_hub.proxies import ProxiesMixin, ProxyHealthPolicy
from identity_hub.read.admin_state import AdminStateMixin
from identity_hub.read.analytics import AnalyticsMixin
from identity_hub.read.background import BackgroundSnapshot
from identity_hub.read.cards import CardsMixin
from identity_hub.read.deliveries import DeliveriesReadMixin
from identity_hub.read.events import EventsReadMixin
from identity_hub.read.human_queue import HumanQueueMixin
from identity_hub.read.leases import LeasesMixin
from identity_hub.read.overview import OverviewMixin
from identity_hub.read.proxies import ProxyReadMixin
from identity_hub.read.search import SearchMixin
from identity_hub.read.services import ServiceDescriptor
from identity_hub.rollups import StatsRollups
from identity_hub.secrets_admin import SecretsAdminMixin
from identity_hub.service import Service
from identity_hub.snapshots import StateSnapshots
from identity_hub.state import StateMixin
from identity_hub.store.base import ServiceRow, SpaceRef, Store
from identity_hub.sync import SyncMixin
from identity_hub.verifications import RequirementsMixin, VerificationsMixin

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from identity_hub.postgres.read_schema import ReadSchema
    from identity_hub.read.overview import SpaceInfo, SpacesMatrix

__all__ = ["IdentityHub", "Retention", "SpaceBase", "SpaceHandle"]


@dataclass(frozen=True)
class Retention:
    """Сроки хранения журналов (ТЗ §8.9, D-004)."""

    events: timedelta = timedelta(days=180)
    lease_history: timedelta = timedelta(days=90)
    lease_logs: timedelta = timedelta(days=30)
    deliveries: timedelta = timedelta(days=30)
    operation_keys: timedelta = timedelta(days=7)
    import_runs: timedelta = timedelta(days=90)
    provider_operations: timedelta = timedelta(days=30)
    rollup_minutes: timedelta = timedelta(hours=48)
    state_snapshots: timedelta = timedelta(days=365)


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
        redact_refs_in_events: bool = False,
        store: Store | None = None,
        proxy_health: ProxyHealthPolicy | None = None,
        lease_logs: LeaseLogPolicy | None = None,
        webhooks: WebhookPolicy | None = None,
        webhook_dev_hosts: Iterable[str] = (),
        telegram_transport: TelegramTransport | None = None,
        maintenance: MaintenanceSchedule | None = None,
        metrics: MetricsHook | None = None,
        timezone: str = "UTC",
    ) -> None:
        if store is None:
            if sessionmaker is None:
                raise ValidationFailed("нужен sessionmaker (PostgreSQL) или store")
            from identity_hub.postgres.store import PgStore

            store = PgStore(sessionmaker, db_schema=db_schema)
        if secrets is None:
            raise ValidationFailed("нужен SecretsProvider (мастер-ключи)")
        self._store = store
        self._envelope = Envelope(secrets)
        self.admin_base_url = admin_base_url.rstrip("/") if admin_base_url else None
        self.retention = retention or Retention()
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValidationFailed(f"unknown IANA timezone: {timezone}") from exc
        self.timezone = timezone
        self.clock: Clock = clock or SystemClock()
        self.proxy_health = proxy_health or ProxyHealthPolicy()
        # sink ih_lease_logs (FR-12.2): подключить `logging.getLogger().addHandler(hub.lease_log_sink)`
        self.lease_log_sink = LeaseLogSink(lease_logs)
        self.dispatcher = Dispatcher(self)
        self.webhook_dev_hosts = frozenset(webhook_dev_hosts)
        self._webhook_dispatcher = WebhookDispatcher(self, webhooks)
        self.dispatcher.add_consumer("webhooks", self._webhook_dispatcher.handle_events)
        self.dispatcher.add_pump(self._webhook_dispatcher.deliver_due)
        if telegram_transport is None:
            from identity_hub.telegram_notify import HttpTelegramTransport

            telegram_transport = HttpTelegramTransport(timeout=self._webhook_dispatcher.policy.timeout)
        self._notification_dispatcher = NotificationDispatcher(
            self, telegram_transport, policy=self._webhook_dispatcher.policy
        )
        self.dispatcher.add_consumer("notifications", self._notification_dispatcher.handle_events)
        self.dispatcher.add_pump(self._notification_dispatcher.deliver_due)
        self._import_runner = ImportRunner(self)
        self.dispatcher.add_pump(self._import_runner.run_once)
        self.maintenance = Maintenance(self, maintenance)
        self.dispatcher.add_pump(self.maintenance.run_due)
        self.doctor = Doctor(self)
        self._metric_rollups = RollupMetricsBuffer(self)
        self.metrics: MetricsHook = CompositeMetrics(self._metric_rollups, metrics or NullMetrics())
        self.dispatcher.add_pump(self._metric_rollups.flush)
        self._metrics_reporter = MetricsReporter(self)
        self.dispatcher.add_pump(self._metrics_reporter.run_due)
        self._rollups = StatsRollups(self)
        self.dispatcher.add_consumer("stats-rollups", self._rollups.handle_events, include_internal=True)
        self.dispatcher.add_pump(self._rollups.run_due)
        self._state_snapshots = StateSnapshots(self)
        self.dispatcher.add_pump(self._state_snapshots.run_due)
        self.events = self.dispatcher  # hub.events.subscribe(name, fn, types=...) — ТЗ §11
        self.strict_events = strict_events  # SecretLeak вместо вырезания секретов в событиях
        # external_ref и labels (телефон, email) не уходят в события и вебхуки (D-152)
        self.redact_refs_in_events = redact_refs_in_events
        self.db_schema = db_schema
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

    @cached_property
    def read(self) -> ReadSchema:
        """Публичная read-схема Core (IMP-12): selectable без секретов для join и `among=`.

        Таблицы получают схему `db_schema` хаба явно; запросы приложения выполняйте как обычно.
        """
        from identity_hub.postgres.read_schema import ReadSchema

        return ReadSchema(self.db_schema)

    @property
    def services(self) -> tuple[Service, ...]:
        return tuple(self._services.values())

    @property
    def sessionmaker(self) -> async_sessionmaker[AsyncSession] | None:
        """Фабрика сессий PostgreSQL, на которой работает хаб; `None` для другого хранилища.

        Нужна, чтобы открыть свою транзакцию и передать её в `space.using(session)`.
        """
        maker: async_sessionmaker[AsyncSession] | None = getattr(self._store, "sessionmaker", None)
        return maker

    @property
    def webhook_policy(self) -> WebhookPolicy:
        """Политика повторов и таймаутов доставки вебхуков и уведомлений."""
        return self._webhook_dispatcher.policy

    async def start(self) -> None:
        """Проверить ревизию схемы и синхронизировать реестр сервисов (FR-1.3, IMP-15).

        Схема БД должна быть ровно той ревизии, что ожидает код: иначе `SchemaIncompatible`
        (без URL и SQL). Миграции `start()` не применяет — это `identity-hub migrate`.
        """
        expected, actual = await self._store.schema_revisions()
        if actual != [expected]:
            raise SchemaIncompatible(expected, ", ".join(actual) or None)
        await self._store.sync_services([ServiceRow(s.name, s.title) for s in self._services.values()])

    # --------------------------------------------------------- пространства

    def space(self, slug: str, title: str | None = None, *, space_id: UUID | None = None) -> SpaceHandle:
        """Пространство создаётся при первом обращении к БД."""
        return SpaceHandle(self, slug, title=title, space_id=space_id)

    async def get_space(self, slug: str) -> SpaceHandle | None:
        """Вернуть существующее пространство, не создавая новое."""
        ref = self._spaces.get(slug)
        if ref is None:
            ref = await self._store.get_space(slug)
            if ref is None:
                return None
            self._spaces[slug] = ref
        return SpaceHandle(self, ref.slug, title=ref.title, space_id=ref.id)

    async def list_spaces(self, allowed_spaces: Collection[str] | None = None) -> tuple[SpaceInfo, ...]:
        """List safe space references, applying an optional authorization scope first."""
        from identity_hub.read.overview import list_spaces

        return await list_spaces(self, allowed_spaces)

    async def spaces_matrix(
        self,
        allowed_spaces: Collection[str] | None = None,
        *,
        service: str | None = None,
    ) -> SpacesMatrix:
        """Return current overview rows for authorized spaces."""
        from identity_hub.read.overview import spaces_matrix

        return await spaces_matrix(self, allowed_spaces, service=service)

    async def service_catalog(
        self, allowed_spaces: Collection[str] | None = None
    ) -> tuple[ServiceDescriptor, ...]:
        """Return deterministic declarations and counts for authorized spaces."""
        from identity_hub.read.services import service_catalog

        return await service_catalog(self, allowed_spaces)

    async def background_snapshot(self, allowed_spaces: Collection[str] | None = None) -> BackgroundSnapshot:
        from identity_hub.read.background import background_snapshot

        return await background_snapshot(self, allowed_spaces)

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
            ref = await self._store.ensure_space(slug, title, space_id, timezone=self.timezone)
            self._spaces[slug] = ref
        return ref


class SpaceHandle(
    AdminStateMixin,
    AnalyticsMixin,
    AdminAuditMixin,
    CardsMixin,
    DeliveriesReadMixin,
    EventsReadMixin,
    HumanQueueMixin,
    LeasesMixin,
    OverviewMixin,
    ProxyReadMixin,
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

    @cached_property
    def imports(self) -> Imports:
        """Импорт identity пачками с отчётом (IMP-23)."""
        return Imports(self)

    @cached_property
    def secrets(self) -> Secrets:
        """Секреты identity вне аренды: метаданные, замена, раскрытие с аудитом (IMP-23)."""
        return Secrets(self)

    @cached_property
    def proxies(self) -> Proxies:
        """Прокси пространства по группам (IMP-23)."""
        return Proxies(self)

    @cached_property
    def pools(self) -> Pools:
        """Пулы аренды: объявление и отзыв аренды (IMP-23)."""
        return Pools(self)

    @cached_property
    def identities(self) -> Identities:
        """Identity пространства: чтение, создание, изменение (IMP-23)."""
        return Identities(self)

    @cached_property
    def auth(self) -> Auth:
        """Вход через Authenticator сервиса и ответ на challenge (IMP-23)."""
        return Auth(self)

    @cached_property
    def sync(self) -> Sync:
        """Сверка регионов identity с платформой через пробы (IMP-23)."""
        return Sync(self)

    @cached_property
    def links(self) -> IdentityLinks:
        """Связи между identity пространства (IMP-23)."""
        return IdentityLinks(self)

    @cached_property
    def data(self) -> IdentityData:
        """Версионируемые данные приложения по неймспейсам identity (IMP-23)."""
        return IdentityData(self)

    @cached_property
    def verifications(self) -> Verifications:
        """Статус факторов, решение человека и требования платформы (IMP-23)."""
        return Verifications(self)

    @cached_property
    def admin(self) -> Admin:
        """Read-модели админки и аудит её действий (IMP-23)."""
        return Admin(self)

    @cached_property
    def analytics(self) -> Analytics:
        """Аналитические отчёты из rollups и снимков состояния (IMP-23)."""
        return Analytics(self)

    @cached_property
    def maintenance(self) -> SpaceMaintenance:
        """Операции обслуживания: истечение challenge и верификаций, переиндексация (IMP-23)."""
        return SpaceMaintenance(self)
