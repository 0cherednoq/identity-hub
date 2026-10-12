"""`space.proxies` — прокси пространства по группам (IMP-23).

«Группа» (`group`) — сохранённый набор прокси, из которого сервис выдаёт адреса
(`ProxyPolicy.group`). Слово «пул» в API означает только пул аренды (`space.pools`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from identity_hub._space import require_actor_and_reason
from identity_hub.api._base import Namespace
from identity_hub.api.batch import MAX_BATCH, BatchResult
from identity_hub.api.identities import OnConflict, Page
from identity_hub.enums import ProxyStatus
from identity_hub.errors import NotFound, ValidationFailed
from identity_hub.identities import UNSET, _Unset
from identity_hub.proxies import parse_proxy_url
from identity_hub.read.search import decode_cursor, encode_cursor
from identity_hub.store.base import NewProxy, ProxyReadRow, ProxyRow

if TYPE_CHECKING:
    from identity_hub.hub import SpaceHandle
    from identity_hub.proxies import ProxyChecker, ProxyHealthPolicy

__all__ = [
    "Proxies",
    "Proxy",
    "ProxyCreate",
    "ProxyCreateManyResult",
    "ProxyCreated",
    "ProxyDeleteManyResult",
    "RecheckReport",
]

MAX_PROXY_PAGE = 200


@dataclass(frozen=True, slots=True)
class Proxy:
    """Прокси без логина и пароля. `bound_count` заполняется только в `list()`."""

    id: UUID
    group: str
    scheme: str
    host: str
    port: int
    status: ProxyStatus
    error_score: float
    fail_streak: int
    next_check_at: datetime | None
    pinned_until: datetime | None
    max_identities: int
    created_at: datetime
    bound_count: int | None = None

    @classmethod
    def from_row(cls, row: ProxyRow | ProxyReadRow) -> Proxy:
        return cls(
            id=row.id,
            group=row.pool,
            scheme=row.scheme,
            host=row.host,
            port=row.port,
            status=ProxyStatus(row.status),
            error_score=row.error_score,
            fail_streak=row.fail_streak,
            next_check_at=row.next_check_at,
            pinned_until=row.pinned_until,
            max_identities=row.max_identities,
            created_at=row.created_at,
            bound_count=row.bound_count if isinstance(row, ProxyReadRow) else None,
        )


@dataclass(frozen=True, slots=True)
class RecheckReport:
    """Итог `proxies.recheck_due`: новый статус каждого перепроверенного прокси."""

    statuses: dict[UUID, ProxyStatus]


@dataclass(frozen=True, slots=True)
class ProxyCreate:
    """Прокси для `proxies.create_many`: адрес по частям, необязательные свой `id` и ёмкость.

    `max_identities=None` — значение из параметра `create_many`. Пароль не попадает в `repr`.
    """

    scheme: str
    host: str
    port: int
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    max_identities: int | None = None
    id: UUID | None = None

    @classmethod
    def from_url(cls, url: str, *, id: UUID | None = None, max_identities: int | None = None) -> ProxyCreate:
        """Запись из URL `схема://логин:пароль@хост:порт`; ошибка разбора — `ValidationFailed`."""
        parsed = parse_proxy_url(url)
        return cls(
            parsed.scheme, parsed.host, parsed.port, parsed.username, parsed.password, max_identities, id
        )


@dataclass(frozen=True, slots=True)
class ProxyCreated:
    """Исход одной записи `create_many`: место во входе, id прокси и создан ли он этой записью."""

    index: int
    id: UUID
    created: bool


@dataclass(frozen=True, slots=True)
class ProxyCreateManyResult:
    """Итог `proxies.create_many`.

    `items` — исход каждой записи в порядке входа. `created` — новые прокси, `skipped` — id уже
    существовавших и повторов внутри пачки (при `on_conflict="skip"`).
    """

    created: list[Proxy]
    skipped: list[UUID]
    items: list[ProxyCreated] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ProxyDeleteManyResult(BatchResult[UUID]):
    """Итог `proxies.delete_many`: удалённые прокси, отказы и общее число отвязанных identity."""

    unbound: int = 0


class Proxies(Namespace):
    """Прокси пространства: `space.proxies`."""

    __slots__ = ()

    # ------------------------------------------------------------------ чтение

    async def get(self, proxy_id: UUID) -> Proxy:
        """Прокси этого пространства или `NotFound`."""
        return Proxy.from_row(await self._space._get_proxy(proxy_id))

    async def list(
        self,
        *,
        group: str | None = None,
        status: ProxyStatus | str | None = None,
        limit: int = 50,
        after: str | None = None,
    ) -> Page[Proxy]:
        """Страница прокси (от новых к старым) с числом привязанных identity."""
        if not 1 <= limit <= MAX_PROXY_PAGE:
            raise ValidationFailed(f"limit: от 1 до {MAX_PROXY_PAGE}")
        before = decode_cursor(after) if after else None
        async with self._space._tx(own=True) as tx:
            rows = await tx.proxy_read_page(
                limit=limit + 1,
                before=before,
                pool=group,
                status=str(status) if status is not None else None,
                capacity=None,
            )
        page = rows[:limit]
        next_cursor = encode_cursor(page[-1].created_at, page[-1].id) if len(rows) > limit else None
        return Page([Proxy.from_row(row) for row in page], next_cursor)

    @property
    def health(self) -> ProxyHealthPolicy:
        """Политика здоровья прокси хаба: пороги ошибок, backoff перепроверки."""
        return self._space._proxy_health

    async def url(self, proxy_id: UUID, *, actor: str, reason: str) -> str:
        """URL с логином и паролем — только для своего сетевого клиента, не для логов и UI.

        Раскрытие аудируется: событие `proxy.secret.revealed` пишется в той же транзакции.
        """
        return await self._space._proxy_url(proxy_id, actor=actor, reason=reason)

    # ------------------------------------------------------------------ создание

    async def create(
        self,
        url: str | None = None,
        *,
        group: str,
        scheme: str | None = None,
        host: str | None = None,
        port: int | None = None,
        username: str | None = None,
        password: str | None = None,
        max_identities: int = 3,
        id: UUID | None = None,
    ) -> Proxy:
        """Добавить прокси в группу; тот же адрес уже есть — `AlreadyExists`."""
        row = self._space._prepare_proxy(
            url, group, scheme, host, port, username, password, max_identities, id
        )
        ((created, _is_new),) = await self._space._create_proxies([row], fail_existing=True)
        return Proxy.from_row(created)

    async def ensure(
        self,
        url: str | None = None,
        *,
        group: str,
        scheme: str | None = None,
        host: str | None = None,
        port: int | None = None,
        username: str | None = None,
        password: str | None = None,
        max_identities: int = 3,
        id: UUID | None = None,
    ) -> tuple[Proxy, bool]:
        """Вернуть существующий прокси с тем же адресом (`False`) или добавить (`True`)."""
        row = self._space._prepare_proxy(
            url, group, scheme, host, port, username, password, max_identities, id
        )
        ((found, created),) = await self._space._create_proxies([row])
        return Proxy.from_row(found), created

    async def create_many(
        self,
        items: Iterable[str | ProxyCreate],
        *,
        group: str,
        max_identities: int = 3,
        on_conflict: OnConflict | str = OnConflict.FAIL,
    ) -> ProxyCreateManyResult:
        """Атомарно добавить прокси одной транзакцией: URL и `ProxyCreate` вперемешку, до 1000.

        `on_conflict` — `OnConflict.FAIL` (существующий адрес или повтор в пачке откатывает всё)
        или `SKIP`. `result.items` сопоставляет каждую запись входа с id прокси. Число запросов
        не зависит от размера пачки.
        """
        try:
            on_conflict = OnConflict(on_conflict)
        except ValueError:
            raise ValidationFailed("on_conflict: fail или skip") from None
        rows = [self._prepare(item, group, max_identities) for item in items]
        if len(rows) > MAX_BATCH:
            raise ValidationFailed(f"пачка: не больше {MAX_BATCH} элементов")
        results = await self._space._create_proxies(rows, fail_existing=on_conflict is OnConflict.FAIL)
        return ProxyCreateManyResult(
            [Proxy.from_row(row) for row, is_new in results if is_new],
            [row.id for row, is_new in results if not is_new],
            [ProxyCreated(index, row.id, is_new) for index, (row, is_new) in enumerate(results)],
        )

    def _prepare(self, item: str | ProxyCreate, group: str, max_identities: int) -> NewProxy:
        if isinstance(item, str):
            return self._space._prepare_proxy(item, group, None, None, None, None, None, max_identities, None)
        if not isinstance(item, ProxyCreate):
            raise ValidationFailed("прокси: нужен URL или ProxyCreate")
        if item.password and not item.username:
            raise ValidationFailed("прокси: пароль без логина не задаётся")
        capacity = max_identities if item.max_identities is None else item.max_identities
        return self._space._prepare_proxy(
            None, group, item.scheme, item.host, item.port, item.username, item.password, capacity, item.id
        )

    # ------------------------------------------------------------------ изменение и удаление

    async def replace_credentials(
        self, proxy_id: UUID, *, username: str | None, password: str | None, actor: str
    ) -> Proxy:
        """Сменить логин и пароль прокси; адрес, статус и привязки остаются. Событие `proxy.updated`."""
        return Proxy.from_row(
            await self._space._replace_proxy_credentials(
                proxy_id, username=username, password=password, actor=actor
            )
        )

    async def update(
        self,
        proxy_id: UUID,
        *,
        scheme: str | None = None,
        host: str | None = None,
        port: int | None = None,
        username: str | _Unset | None = UNSET,
        password: str | _Unset | None = UNSET,
        actor: str,
    ) -> Proxy:
        """Сменить адрес прокси (схему, хост, порт); статус, группа и привязки остаются.

        Незаданный параметр не меняется. Логин и пароль можно сменить тем же вызовом; `None`
        убирает значение. Тот же адрес с тем же логином у другого прокси — `AlreadyExists`.
        Событие `proxy.updated` с изменёнными полями и прежним адресом.
        """
        return Proxy.from_row(
            await self._space._update_proxy(
                proxy_id,
                scheme=scheme,
                host=host,
                port=port,
                username=username,
                password=password,
                actor=actor,
            )
        )

    async def move(self, proxy_id: UUID, *, group: str, actor: str) -> Proxy:
        """Перенести прокси в другую группу; привязки сохраняются. Событие `proxy.updated`."""
        return Proxy.from_row(await self._space._move_proxy(proxy_id, group=group, actor=actor))

    async def delete(self, proxy_id: UUID, *, actor: str, unbind: bool = False) -> int:
        """Удалить прокси. С привязанными identity — `ValidationFailed`; `unbind=True` отвязывает их.

        Возвращает число отвязанных identity. События: `proxy.binding.changed` на каждую, `proxy.deleted`.
        """
        return await self._space._delete_proxy(proxy_id, actor=actor, unbind=unbind)

    async def delete_many(
        self, ids: Iterable[UUID], *, actor: str, unbind: bool = False, atomic: bool = False
    ) -> ProxyDeleteManyResult:
        """Удалить прокси одной транзакцией, до 1000. События — как у `delete`, на каждый прокси.

        Прокси с привязанными identity без `unbind=True` попадает в `rejected`
        (`validation_error`), остальные удаляются; `atomic=True` — первый отказ отменяет всё.
        """
        unbound: dict[UUID, int] = {}

        async def delete(handle: SpaceHandle, proxy_id: UUID) -> None:
            unbound[proxy_id] = await handle._delete_proxy(proxy_id, actor=actor, unbind=unbind)

        result = await self._space._run_batch(ids, delete, atomic=atomic)
        return ProxyDeleteManyResult(
            result.applied, result.rejected, sum(unbound[proxy_id] for proxy_id in result.applied)
        )

    # ------------------------------------------------------------------ здоровье и привязка

    async def report_failure(
        self, proxy_id: UUID, *, weight: float = 1.0, actor: str, raw_status: str | None = None
    ) -> ProxyStatus:
        """Сетевая ошибка через прокси; при превышении порога — карантин."""
        return ProxyStatus(
            await self._space._report_proxy_failure(
                proxy_id, weight=weight, actor=actor, raw_status=raw_status
            )
        )

    async def report_success(self, proxy_id: UUID) -> None:
        """Успешный запрос: `error_score` затухает."""
        await self._space._report_proxy_success(proxy_id)

    async def report_many(
        self, outcomes: Mapping[UUID, bool | float], *, actor: str
    ) -> dict[UUID, ProxyStatus]:
        """Результаты своих проверок пачкой, до 1000: `True` — успех, `False` или вес — ошибка.

        Возвращает статус каждого прокси после отчёта; неизвестный прокси в ответ не попадает.
        Как и `report_failure`, это факты внешнего мира: каждый отчёт фиксируется своей
        транзакцией, а не транзакцией `space.using(session)`.
        """
        if len(outcomes) > MAX_BATCH:
            raise ValidationFailed(f"пачка: не больше {MAX_BATCH} элементов")
        statuses: dict[UUID, ProxyStatus] = {}
        for proxy_id, outcome in sorted(outcomes.items(), key=lambda item: item[0]):
            try:
                if outcome is True:
                    await self._space._report_proxy_success(proxy_id)
                    statuses[proxy_id] = (await self.get(proxy_id)).status
                else:
                    weight = 1.0 if outcome is False else float(outcome)
                    statuses[proxy_id] = await self.report_failure(proxy_id, weight=weight, actor=actor)
            except NotFound:
                continue
        return statuses

    async def recheck(self, proxy_id: UUID, checker: ProxyChecker, *, actor: str) -> ProxyStatus:
        """Ручная перепроверка: до двух проверок подряд для выхода из карантина."""
        return ProxyStatus(await self._space._recheck_proxy(proxy_id, checker, actor=actor))

    async def recheck_due(self, checker: ProxyChecker, *, limit: int = 50) -> RecheckReport:
        """Перепроверить прокси, у которых наступил срок: `quarantined` и, раз в `max_backoff`, `dead`.

        Проверки идут параллельно (`ProxyHealthPolicy.check_concurrency`). Закреплённые прокси
        не трогаются. Сбой чекера и одновременный отказ всей группы отказом прокси не считаются.
        """
        results = await self._space._recheck_proxies(checker, limit=limit)
        return RecheckReport({proxy_id: ProxyStatus(status) for proxy_id, status in results.items()})

    async def recheck_group(
        self, group: str, checker: ProxyChecker, *, actor: str, limit: int = 500
    ) -> RecheckReport:
        """Перепроверить сейчас все недоступные прокси группы — например, когда провайдер вернулся."""
        results = await self._space._recheck_group(group, checker, actor=actor, limit=limit)
        return RecheckReport({proxy_id: ProxyStatus(status) for proxy_id, status in results.items()})

    async def override(
        self,
        proxy_id: UUID,
        status: ProxyStatus | str,
        *,
        reason: str,
        actor: str,
        pin_for: timedelta | None = None,
    ) -> ProxyStatus:
        """Ручная смена статуса прокси; `pin_for` закрепляет его от автоматических изменений."""
        return ProxyStatus(
            await self._space._override_proxy(proxy_id, status, reason=reason, actor=actor, pin_for=pin_for)
        )

    async def override_many(
        self,
        statuses: Mapping[UUID, ProxyStatus | str],
        *,
        reason: str,
        actor: str,
        pin_for: timedelta | None = None,
        atomic: bool = False,
    ) -> BatchResult[UUID]:
        """Ручная смена статуса пачкой, до 1000: у каждого прокси свой статус.

        Причина, автор и `pin_for` общие. События — как у `override`, на каждый прокси.
        Неизвестный статус или прокси — отказ своей строки; `atomic=True` отменяет всё.
        """
        actor, reason = require_actor_and_reason(actor, reason, "смена статуса прокси")
        wanted = dict(statuses)

        async def override(handle: SpaceHandle, proxy_id: UUID) -> None:
            try:
                status = ProxyStatus(wanted[proxy_id])
            except ValueError:
                raise ValidationFailed("статус прокси: alive, quarantined или dead") from None
            await handle._override_proxy(proxy_id, status, reason=reason, actor=actor, pin_for=pin_for)

        return await self._space._run_batch(wanted, override, atomic=atomic)

    async def rebind(self, identity_id: UUID, proxy_id: UUID, *, reason: str, actor: str) -> bool:
        """Перепривязать identity к другому прокси (sticky)."""
        return await self._space._rebind_proxy(identity_id, proxy_id, reason=reason, actor=actor)
