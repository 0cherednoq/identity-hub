"""`space.proxies` — прокси пространства по группам (IMP-23).

«Группа» (`group`) — сохранённый набор прокси, из которого сервис выдаёт адреса
(`ProxyPolicy.group`). Слово «пул» в API означает только пул аренды (`space.pools`).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from identity_hub.api._base import Namespace
from identity_hub.api.identities import OnConflict, Page
from identity_hub.enums import ProxyStatus
from identity_hub.errors import ValidationFailed
from identity_hub.read.search import decode_cursor, encode_cursor
from identity_hub.store.base import ProxyReadRow, ProxyRow

if TYPE_CHECKING:
    from identity_hub.proxies import ProxyChecker, ProxyHealthPolicy

__all__ = ["Proxies", "Proxy", "ProxyCreateManyResult"]

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
class ProxyCreateManyResult:
    """Итог `proxies.create_many`: новые прокси и id уже существовавших (при `on_conflict="skip"`)."""

    created: list[Proxy]
    skipped: list[UUID]


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
        ((proxy_id, _created),) = await self._space._insert_proxies([row], fail_existing=True)
        return await self.get(proxy_id)

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
        ((proxy_id, created),) = await self._space._insert_proxies([row])
        return await self.get(proxy_id), created

    async def create_many(
        self,
        urls: Iterable[str],
        *,
        group: str,
        max_identities: int = 3,
        on_conflict: OnConflict | str = OnConflict.FAIL,
    ) -> ProxyCreateManyResult:
        """Атомарно добавить прокси одной транзакцией; `on_conflict` — `OnConflict.FAIL` или `SKIP`."""
        try:
            on_conflict = OnConflict(on_conflict)
        except ValueError:
            raise ValidationFailed("on_conflict: fail или skip") from None
        rows = [
            self._space._prepare_proxy(url, group, None, None, None, None, None, max_identities, None)
            for url in urls
        ]
        results = await self._space._insert_proxies(rows, fail_existing=on_conflict is OnConflict.FAIL)
        created = [await self.get(proxy_id) for proxy_id, is_new in results if is_new]
        return ProxyCreateManyResult(created, [proxy_id for proxy_id, is_new in results if not is_new])

    # ------------------------------------------------------------------ изменение и удаление

    async def replace_credentials(
        self, proxy_id: UUID, *, username: str | None, password: str | None, actor: str
    ) -> Proxy:
        """Сменить логин и пароль прокси; адрес, статус и привязки остаются. Событие `proxy.updated`."""
        await self._space._replace_proxy_credentials(
            proxy_id, username=username, password=password, actor=actor
        )
        return await self.get(proxy_id)

    async def move(self, proxy_id: UUID, *, group: str, actor: str) -> Proxy:
        """Перенести прокси в другую группу; привязки сохраняются. Событие `proxy.updated`."""
        await self._space._move_proxy(proxy_id, group=group, actor=actor)
        return await self.get(proxy_id)

    async def delete(self, proxy_id: UUID, *, actor: str, unbind: bool = False) -> int:
        """Удалить прокси. С привязанными identity — `ValidationFailed`; `unbind=True` отвязывает их.

        Возвращает число отвязанных identity. События: `proxy.binding.changed` на каждую, `proxy.deleted`.
        """
        return await self._space._delete_proxy(proxy_id, actor=actor, unbind=unbind)

    # ------------------------------------------------------------------ здоровье и привязка

    async def report_failure(
        self, proxy_id: UUID, *, weight: float = 1.0, actor: str = "worker", raw_status: str | None = None
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

    async def recheck(self, proxy_id: UUID, checker: ProxyChecker, *, actor: str) -> ProxyStatus:
        """Ручная перепроверка: до двух проверок подряд для выхода из карантина."""
        return ProxyStatus(await self._space._recheck_proxy(proxy_id, checker, actor=actor))

    async def recheck_due(self, checker: ProxyChecker, *, limit: int = 50) -> dict[UUID, ProxyStatus]:
        """Перепроверить прокси, у которых наступил срок: `quarantined` и, раз в `max_backoff`, `dead`.

        Проверки идут параллельно (`ProxyHealthPolicy.check_concurrency`). Закреплённые прокси
        не трогаются. Сбой чекера и одновременный отказ всей группы отказом прокси не считаются.
        """
        results = await self._space._recheck_proxies(checker, limit=limit)
        return {proxy_id: ProxyStatus(status) for proxy_id, status in results.items()}

    async def recheck_group(
        self, group: str, checker: ProxyChecker, *, actor: str, limit: int = 500
    ) -> dict[UUID, ProxyStatus]:
        """Перепроверить сейчас все недоступные прокси группы — например, когда провайдер вернулся."""
        results = await self._space._recheck_group(group, checker, actor=actor, limit=limit)
        return {proxy_id: ProxyStatus(status) for proxy_id, status in results.items()}

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

    async def rebind(self, identity_id: UUID, proxy_id: UUID, *, reason: str, actor: str) -> bool:
        """Перепривязать identity к другому прокси (sticky)."""
        return await self._space._rebind_proxy(identity_id, proxy_id, reason=reason, actor=actor)
