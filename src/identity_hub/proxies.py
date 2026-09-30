"""Прокси: пулы, секреты, здоровье (ТЗ FR-8.1, 8.4, §3.7).

Хост и порт открыты (админка, дедупликация), логин и пароль зашифрованы. Здоровье — регион
PROXY_HEALTH в колонках ih_proxies (D-116): ошибки → карантин, перепроверка с экспоненциальным
backoff, `dead` после предела, выход из тяжёлых состояний — по двум успешным проверкам подряд.
Сетевые проверки выполняются вне транзакций (T-5).
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any
from urllib.parse import quote, unquote, urlsplit
from uuid import UUID

from pydantic import BaseModel, SecretStr

from identity_hub._ids import new_id
from identity_hub._space import SpaceBase, logger
from identity_hub.crypto import associated_data
from identity_hub.domain import regions as machine
from identity_hub.domain.region_specs import PROXY_HEALTH
from identity_hub.domain.regions import Change, Command, Observation, Outcome, RegionDecision, RegionState
from identity_hub.enums import ChangeSource, ProxyStatus
from identity_hub.errors import ConcurrentModification, LeaseUnavailable, NotFound, ValidationFailed
from identity_hub.events import EventDraft
from identity_hub.store.base import NewProxy, ProxyRow, StoreTx

__all__ = [
    "SCHEMES",
    "ProxiesMixin",
    "ProxyChecker",
    "ProxyHealthPolicy",
    "ProxySecret",
    "fingerprint",
    "parse_proxy_url",
]

SCHEMES = frozenset({"http", "https", "socks4", "socks5", "socks5h", "mtproxy"})
ProxyChecker = Callable[[str], Awaitable[bool]]
"""Проверка прокси пользователем: URL с логином и паролем → работает ли."""


@dataclass(frozen=True)
class ProxyHealthPolicy:
    error_threshold: float = 3.0
    base_backoff: timedelta = timedelta(seconds=60)
    max_backoff: timedelta = timedelta(hours=1)
    dead_after: int = 5  # неудачных перепроверок подряд
    success_decay: float = 0.5

    def backoff(self, fail_streak: int) -> timedelta:
        grown: timedelta = self.base_backoff * (2 ** min(fail_streak, 30))
        return min(grown, self.max_backoff)


class ProxySecret(BaseModel):
    username: str | None = None
    password: SecretStr | None = None


@dataclass(frozen=True, slots=True)
class ParsedProxy:
    scheme: str
    host: str
    port: int
    username: str | None
    password: str | None


def parse_proxy_url(url: str) -> ParsedProxy:
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in SCHEMES:
        raise ValidationFailed(f"прокси: схема {scheme!r} не поддерживается ({', '.join(sorted(SCHEMES))})")
    if not parts.hostname or parts.port is None:
        raise ValidationFailed("прокси: нужны хост и порт")
    return ParsedProxy(
        scheme=scheme,
        host=parts.hostname.lower(),
        port=parts.port,
        username=unquote(parts.username) if parts.username else None,
        password=unquote(parts.password) if parts.password else None,
    )


def fingerprint(scheme: str, host: str, port: int, username: str | None) -> str:
    """hash(scheme, host, port, username) — дедупликация в пространстве (FR-8.1)."""
    raw = f"{scheme}|{host.lower()}|{port}|{username or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _region_state(row: ProxyRow) -> RegionState:
    return RegionState(
        state=row.status,
        changed_at=row.status_changed_at,
        change_source=ChangeSource(row.status_source),
        pinned_until=row.pinned_until,
        pending_observation=row.pending_observation,
    )


class ProxiesMixin(SpaceBase):
    # --------------------------------------------------------- добавление

    async def add_proxy(
        self,
        url: str | None = None,
        *,
        pool: str,
        scheme: str | None = None,
        host: str | None = None,
        port: int | None = None,
        username: str | None = None,
        password: str | None = None,
        max_identities: int = 3,
        id: UUID | None = None,
    ) -> tuple[UUID, bool]:
        """Добавить прокси в пул; повтор с тем же fingerprint возвращает существующий (id, False)."""
        (result,) = await self._add_proxies(
            [self._prepare_proxy(url, pool, scheme, host, port, username, password, max_identities, id)]
        )
        return result

    async def add_proxies(
        self, urls: Iterable[str], *, pool: str, max_identities: int = 3
    ) -> list[tuple[UUID, bool]]:
        rows = [
            self._prepare_proxy(u, pool, None, None, None, None, None, max_identities, None) for u in urls
        ]
        return await self._add_proxies(rows)

    def _prepare_proxy(
        self,
        url: str | None,
        pool: str,
        scheme: str | None,
        host: str | None,
        port: int | None,
        username: str | None,
        password: str | None,
        max_identities: int,
        proxy_id: UUID | None,
    ) -> NewProxy:
        if url is not None:
            parsed = parse_proxy_url(url)
        else:
            if not (scheme and host and port):
                raise ValidationFailed("прокси: нужен url или scheme/host/port")
            parsed = parse_proxy_url(f"{scheme}://{host}:{port}")
            parsed = replace(parsed, username=username, password=password)
        if not pool or len(pool) > 128:
            raise ValidationFailed("пул прокси: от 1 до 128 символов")
        if max_identities < 1:
            raise ValidationFailed("max_identities ≥ 1")
        proxy_id = proxy_id or new_id()
        secret = ProxySecret(
            username=parsed.username, password=SecretStr(parsed.password) if parsed.password else None
        )
        payload = self.hub.envelope.seal_model(secret, associated_data("ih_proxies", proxy_id))
        return NewProxy(
            id=proxy_id,
            pool=pool,
            scheme=parsed.scheme,
            host=parsed.host,
            port=parsed.port,
            fingerprint=fingerprint(parsed.scheme, parsed.host, parsed.port, parsed.username),
            max_identities=max_identities,
            secret=payload,
        )

    async def _add_proxies(self, rows: list[NewProxy]) -> list[tuple[UUID, bool]]:
        async with self._tx() as tx:
            return [await tx.insert_proxy(row) for row in rows]

    # --------------------------------------------------------- чтение

    async def get_proxy(self, proxy_id: UUID) -> ProxyRow:
        async with self._tx(own=True) as tx:
            return await self._proxy(tx, proxy_id)

    async def _proxy_url(self, proxy_id: UUID) -> str:
        """URL с расшифрованными логином и паролем — только для аренды и проверок, не для админки."""
        return self.proxy_url_of(await self.get_proxy(proxy_id))

    def proxy_url_of(self, row: ProxyRow) -> str:
        secret = self.hub.envelope.open_model(ProxySecret, row.secret, associated_data("ih_proxies", row.id))
        auth = ""
        if secret.username:
            auth = quote(secret.username, safe="")
            if secret.password:
                auth += ":" + quote(secret.password.get_secret_value(), safe="")
            auth += "@"
        return f"{row.scheme}://{auth}{row.host}:{row.port}"

    @staticmethod
    async def _proxy(tx: StoreTx, proxy_id: UUID) -> ProxyRow:
        row = await tx.get_proxy(proxy_id)
        if row is None:
            raise NotFound("proxy", proxy_id)
        return row

    # --------------------------------------------------------- здоровье

    @property
    def proxy_health(self) -> ProxyHealthPolicy:
        policy: ProxyHealthPolicy = self.hub.proxy_health
        return policy

    async def report_proxy_failure(
        self, proxy_id: UUID, *, weight: float = 1.0, actor: str = "worker", raw_status: str | None = None
    ) -> str:
        """Ошибка по вине прокси (FR-8.4): счёт ошибок; при пороге — карантин с перепроверкой."""
        policy = self.proxy_health
        async with self._tx(own=True) as tx:
            row = await self._proxy(tx, proxy_id)
            if row.status != ProxyStatus.ALIVE:
                return row.status
            score = row.error_score + weight
            if score < policy.error_threshold:
                await tx.update_proxy(proxy_id, {"error_score": score})
                return row.status
            now = await tx.now()
            decision = await self._apply(
                tx,
                row,
                Observation(
                    ProxyStatus.QUARANTINED,
                    observed_at=now,
                    source=ChangeSource.ERROR,
                    raw_status=raw_status,
                    actor=actor,
                ),
                {"error_score": score, "fail_streak": 0, "next_check_at": now + policy.base_backoff},
            )
            return decision.state.state

    async def report_proxy_success(self, proxy_id: UUID) -> None:
        async with self._tx(own=True) as tx:
            row = await self._proxy(tx, proxy_id)
            if row.error_score > 0 and row.status == ProxyStatus.ALIVE:
                score = row.error_score * self.proxy_health.success_decay
                await tx.update_proxy(proxy_id, {"error_score": score if score >= 0.01 else 0.0})

    async def recheck_proxies(self, checker: ProxyChecker, *, limit: int = 50) -> dict[UUID, str]:
        """Плановая перепроверка прокси в карантине, у которых наступил срок (обслуживание)."""
        async with self._tx(own=True) as tx:
            due = await tx.due_proxies(limit)
        return {
            proxy_id: await self._recheck_once(proxy_id, checker, actor="maintenance") for proxy_id in due
        }

    async def recheck_proxy(self, proxy_id: UUID, checker: ProxyChecker, *, actor: str) -> str:
        """Ручная перепроверка: до двух проверок подряд — выход из карантина или `dead` подтверждается."""
        status = await self._recheck_once(proxy_id, checker, actor=actor, manual=True)
        row = await self.get_proxy(proxy_id)
        if row.pending_observation == ProxyStatus.ALIVE:
            status = await self._recheck_once(proxy_id, checker, actor=actor, manual=True)
        return status

    async def _recheck_once(
        self, proxy_id: UUID, checker: ProxyChecker, *, actor: str, manual: bool = False
    ) -> str:
        row = await self.get_proxy(proxy_id)
        if row.status == ProxyStatus.ALIVE or (row.status == ProxyStatus.DEAD and not manual):
            return row.status
        url = self.proxy_url_of(row)
        try:
            ok = bool(await checker(url))
        except Exception:
            logger.warning("проверка прокси %s упала", proxy_id, exc_info=True)
            ok = False
        policy = self.proxy_health
        try:
            async with self._tx(own=True) as tx:
                current = await self._proxy(tx, proxy_id)
                if current.status != row.status:
                    return current.status  # изменился параллельно
                now = await tx.now()
                if ok:
                    extra: dict[str, Any] = {"checked_at": now, "next_check_at": now + policy.base_backoff}
                    decision = await self._apply(
                        tx, current, Observation(ProxyStatus.ALIVE, observed_at=now, actor=actor), extra
                    )
                    if decision.state.state == ProxyStatus.ALIVE:
                        await tx.update_proxy(
                            proxy_id, {"fail_streak": 0, "error_score": 0.0, "next_check_at": None}
                        )
                    return decision.state.state
                streak = current.fail_streak + 1
                extra = {
                    "checked_at": now,
                    "fail_streak": streak,
                    "next_check_at": now + policy.backoff(streak),
                }
                if current.status == ProxyStatus.QUARANTINED and streak >= policy.dead_after:
                    extra["next_check_at"] = None
                    change: Change = Command(
                        ProxyStatus.DEAD, actor=actor, reason=f"{streak} неудачных перепроверок"
                    )
                else:
                    change = Observation(
                        current.status, observed_at=now, actor=actor
                    )  # сбросить ожидание выхода
                return (await self._apply(tx, current, change, extra)).state.state
        except ConcurrentModification:
            return (await self.get_proxy(proxy_id)).status

    async def override_proxy(
        self,
        proxy_id: UUID,
        status: ProxyStatus | str,
        *,
        reason: str,
        actor: str,
        pin_for: timedelta | None = None,
    ) -> str:
        """Ручная правка здоровья прокси (FR-9.1): причина и автор обязательны."""
        async with self._tx() as tx:
            row = await self._proxy(tx, proxy_id)
            extra: dict[str, Any] = {}
            if str(status) == ProxyStatus.ALIVE:
                extra = {"fail_streak": 0, "error_score": 0.0, "next_check_at": None}
            elif str(status) == ProxyStatus.QUARANTINED:
                extra = {"next_check_at": await tx.now() + self.proxy_health.base_backoff}
            change = machine.Override(to=str(status), reason=reason, actor=actor, pin_for=pin_for)
            return (await self._apply(tx, row, change, extra)).state.state

    async def rebind_proxy(self, identity_id: UUID, proxy_id: UUID, *, reason: str, actor: str) -> bool:
        """Move an account's sticky binding without exceeding target capacity."""
        if not reason.strip():
            raise ValidationFailed("proxy rebind requires a reason")
        async with self._tx() as tx:
            identity = await tx.get_identity(identity_id)
            if identity is None:
                raise NotFound("identity", identity_id)
            target = await self._proxy(tx, proxy_id)
            if target.status != ProxyStatus.ALIVE:
                raise ValidationFailed("target proxy must be alive")
            result = await tx.rebind_proxy(identity_id, proxy_id)
            if result is None:
                raise LeaseUnavailable("target proxy has no available capacity")
            previous, claimed = result
            if previous == claimed.id:
                return False
            await self._emit(
                tx,
                EventDraft(
                    "proxy.binding.changed",
                    actor=actor,
                    service=identity.service,
                    identity_id=identity_id,
                    proxy_id=claimed.id,
                    data={
                        "from_proxy_id": str(previous) if previous else None,
                        "to_proxy_id": str(claimed.id),
                        "reason": reason,
                        "pool": claimed.pool,
                    },
                ),
            )
            return True

    async def _apply(
        self, tx: StoreTx, row: ProxyRow, change: Change, extra: dict[str, Any]
    ) -> RegionDecision:
        now = await tx.now()
        current = _region_state(row)
        decision = machine.apply(PROXY_HEALTH, current, change, now)
        if decision.outcome is Outcome.STALE:
            return decision
        new = decision.state
        values = {
            **extra,
            "status": new.state,
            "status_changed_at": new.changed_at,
            "status_source": str(new.change_source),
            "pinned_until": new.pinned_until,
            "pending_observation": new.pending_observation,
        }
        if not await tx.update_proxy(row.id, values, {"status": row.status}):
            raise ConcurrentModification("proxy", row.id)
        if decision.events:
            await self._emit(
                tx,
                *(
                    EventDraft(
                        e.type,
                        actor=new.changed_by or "system",
                        data={**e.data, "pool": row.pool, "host": row.host, "port": row.port},
                        severity=e.severity,
                        proxy_id=row.id,
                    )
                    for e in decision.events
                ),
            )
        return decision
