"""Прокси: пулы, секреты, здоровье (ТЗ FR-8.1, 8.4, §3.7).

Хост и порт открыты (админка, дедупликация), логин и пароль зашифрованы. Здоровье — регион
PROXY_HEALTH в колонках ih_proxies (D-116): ошибки → карантин, перепроверка с экспоненциальным
backoff, `dead` после предела, выход из тяжёлых состояний — по двум успешным проверкам подряд.
Сетевые проверки выполняются вне транзакций (T-5).
"""

from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any
from urllib.parse import quote, unquote, urlsplit
from uuid import UUID

from pydantic import BaseModel, SecretStr

from identity_hub._ids import new_id
from identity_hub._space import SpaceBase, logger, require_actor_and_reason
from identity_hub.crypto import associated_data
from identity_hub.domain import regions as machine
from identity_hub.domain.region_specs import PROXY_HEALTH
from identity_hub.domain.regions import Change, Command, Observation, Outcome, RegionDecision, RegionState
from identity_hub.enums import ChangeSource, LeaseUnavailableReason, ProxyStatus
from identity_hub.errors import (
    AlreadyExists,
    ConcurrentModification,
    LeaseUnavailable,
    NotFound,
    ValidationFailed,
    describe_error,
)
from identity_hub.events import EventDraft
from identity_hub.store.base import NewProxy, ProxyRow, StoreTx

__all__ = [
    "SCHEMES",
    "ProxiesMixin",
    "ProxyChecker",
    "ProxyHealthPolicy",
    "ProxySecret",
    "fingerprint_value",
    "legacy_fingerprint",
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
    jitter: float = 0.2  # доля случайной добавки к паузе: прокси одной группы не проверяются залпом
    check_timeout: float = 15.0  # секунд на один вызов чекера; превышение — отказ прокси
    check_concurrency: int = 8  # одновременных проверок в recheck_due и recheck_group

    def backoff(self, fail_streak: int) -> timedelta:
        grown: timedelta = self.base_backoff * (2 ** min(fail_streak, 30))
        return min(grown, self.max_backoff)

    def delay(self, fail_streak: int) -> timedelta:
        """Пауза до следующей перепроверки: `backoff` со случайной добавкой до `jitter`."""
        return self._jittered(self.backoff(fail_streak))

    def dead_delay(self) -> timedelta:
        """Как часто автоматически перепроверяется `dead`: раз в `max_backoff` (с jitter)."""
        return self._jittered(self.max_backoff)

    def _jittered(self, base: timedelta) -> timedelta:
        return base + base * random.uniform(0, max(0.0, self.jitter))


class ProxySecret(BaseModel):
    username: str | None = None
    password: SecretStr | None = None


@dataclass(frozen=True, slots=True)
class ParsedProxy:
    scheme: str
    host: str
    port: int
    username: str | None
    password: str | None = field(repr=False)


def parse_proxy_url(url: str) -> ParsedProxy:
    """Разобрать URL прокси. Любая ошибка разбора — `ValidationFailed` без фрагментов URL."""
    parsed = _split_proxy_url(url)
    if parsed is None:  # вне except: ValueError урлсплита несёт кусок пароля и не идёт в цепочку
        raise ValidationFailed("прокси: некорректный URL")
    scheme, host, port, username, password = parsed
    if scheme not in SCHEMES:
        raise ValidationFailed(f"прокси: схема не поддерживается ({', '.join(sorted(SCHEMES))})")
    if not host or not port:  # порт 0 urlsplit принимает, но подключиться к нему нельзя
        raise ValidationFailed("прокси: нужны хост и порт")
    return ParsedProxy(scheme=scheme, host=host, port=port, username=username, password=password)


def _split_proxy_url(url: str) -> tuple[str, str | None, int | None, str | None, str | None] | None:
    try:
        parts = urlsplit(url)
        return (
            (parts.scheme or "").lower(),
            parts.hostname.lower() if parts.hostname else None,
            parts.port,
            unquote(parts.username) if parts.username else None,
            unquote(parts.password) if parts.password else None,
        )
    except ValueError:
        return None


def fingerprint_value(scheme: str, host: str, port: int, username: str | None) -> str:
    """Что подписывает HMAC-отпечаток прокси: дедупликация в пространстве (FR-8.1, D-207)."""
    return f"proxy:v1|{scheme}|{host.lower()}|{port}|{username or ''}"


def legacy_fingerprint(scheme: str, host: str, port: int, username: str | None) -> str:
    """Отпечаток до миграции 0018 — SHA-256 без ключа (R2-S16); только для поиска до пересчёта."""
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

    async def _add_proxy(
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
        (result,) = await self._insert_proxies(
            [self._prepare_proxy(url, pool, scheme, host, port, username, password, max_identities, id)]
        )
        return result

    async def _add_proxies(
        self, urls: Iterable[str], *, pool: str, max_identities: int = 3
    ) -> list[tuple[UUID, bool]]:
        rows = [
            self._prepare_proxy(u, pool, None, None, None, None, None, max_identities, None) for u in urls
        ]
        return await self._insert_proxies(rows)

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
        payload = self.hub._envelope.seal_model(secret, associated_data("ih_proxies", proxy_id))
        tag, key_id, aliases = self._proxy_fingerprints(
            parsed.scheme, parsed.host, parsed.port, parsed.username
        )
        return NewProxy(
            id=proxy_id,
            pool=pool,
            scheme=parsed.scheme,
            host=parsed.host,
            port=parsed.port,
            fingerprint=tag,
            max_identities=max_identities,
            secret=payload,
            fingerprint_key_id=key_id,
            aliases=aliases,
        )

    def _proxy_fingerprints(
        self, scheme: str, host: str, port: int, username: str | None
    ) -> tuple[str, str, tuple[str, ...]]:
        """(отпечаток активным ключом, его key_id, отпечатки прежними ключами и SHA-256 до 0018)."""
        keyring = self.hub._fingerprints
        active = keyring.active_key_id
        tags = keyring.tags(fingerprint_value(scheme, host, port, username))
        aliases = tuple(tag for key_id, tag in sorted(tags.items()) if key_id != active)
        return tags[active], active, (*aliases, legacy_fingerprint(scheme, host, port, username))

    async def _insert_proxies(
        self, rows: list[NewProxy], *, fail_existing: bool = False
    ) -> list[tuple[UUID, bool]]:
        """Вставить одной транзакцией; `fail_existing` — любой существующий адрес откатывает всё."""
        async with self._tx() as tx:
            results = [await tx.insert_proxy(row) for row in rows]
            existing = [
                f"{row.scheme}://{row.host}:{row.port}"
                for row, (_id, created) in zip(rows, results, strict=True)
                if not created
            ]
            if fail_existing and existing:
                raise AlreadyExists("proxy", ", ".join(existing))
            return results

    # --------------------------------------------------------- чтение

    async def _get_proxy(self, proxy_id: UUID) -> ProxyRow:
        async with self._tx(own=True) as tx:
            return await self._proxy(tx, proxy_id)

    async def _proxy_url(self, proxy_id: UUID, *, actor: str, reason: str) -> str:
        """URL с расшифрованными логином и паролем вне аренды.

        Событие аудита пишется в той же транзакции, что и чтение (S-7): без записи нет и раскрытия.
        """
        actor, reason = require_actor_and_reason(actor, reason, "раскрытие URL прокси")
        async with self._tx(own=True) as tx:
            row = await self._proxy(tx, proxy_id)
            url = self._proxy_url_of(row)
            await self._emit(
                tx,
                EventDraft("proxy.secret.revealed", actor=actor, proxy_id=row.id, data={"reason": reason}),
            )
        return url

    def _proxy_url_of(self, row: ProxyRow) -> str:
        secret = self.hub._envelope.open_model(ProxySecret, row.secret, associated_data("ih_proxies", row.id))
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
    def _proxy_health(self) -> ProxyHealthPolicy:
        policy: ProxyHealthPolicy = self.hub.proxy_health
        return policy

    async def _report_proxy_failure(
        self, proxy_id: UUID, *, weight: float = 1.0, actor: str = "worker", raw_status: str | None = None
    ) -> str:
        """Ошибка по вине прокси (FR-8.4): счёт ошибок; при пороге — карантин с перепроверкой."""
        policy = self._proxy_health
        async with self._tx(own=True) as tx:
            # сложение выполняет хранилище одной командой: параллельные сообщения не теряются (R2-F23)
            score = await tx.add_proxy_error(proxy_id, weight)
            row = await self._proxy(tx, proxy_id)
            if score is None or score < policy.error_threshold:
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

    async def _report_proxy_success(self, proxy_id: UUID) -> None:
        async with self._tx(own=True) as tx:
            row = await self._proxy(tx, proxy_id)
            if row.error_score > 0 and row.status == ProxyStatus.ALIVE:
                score = row.error_score * self._proxy_health.success_decay
                await tx.update_proxy(proxy_id, {"error_score": score if score >= 0.01 else 0.0})

    async def _recheck_proxies(self, checker: ProxyChecker, *, limit: int = 50) -> dict[UUID, str]:
        """Плановая перепроверка: `quarantined` и (редко) `dead`, у которых наступил срок."""
        async with self._tx(own=True) as tx:
            due = await tx.due_proxies(limit)
        return await self._recheck_batch(due, checker, actor="maintenance", manual=False)

    async def _recheck_group(
        self, group: str, checker: ProxyChecker, *, actor: str, limit: int = 500
    ) -> dict[UUID, str]:
        """Массовая ручная перепроверка всех недоступных прокси группы, не дожидаясь срока."""
        async with self._tx(own=True) as tx:
            page = await tx.proxy_read_page(limit=limit, before=None, pool=group, status=None, capacity=None)
        waiting = [row.id for row in page if row.status != ProxyStatus.ALIVE]
        results = await self._recheck_batch(waiting, checker, actor=actor, manual=True)
        # выход из карантина требует двух успешных проверок подряд — как у recheck одного прокси
        again = [proxy_id for proxy_id in results if await self._awaits_confirmation(proxy_id)]
        results.update(await self._recheck_batch(again, checker, actor=actor, manual=True))
        return results

    async def _awaits_confirmation(self, proxy_id: UUID) -> bool:
        return (await self._get_proxy(proxy_id)).pending_observation == ProxyStatus.ALIVE

    async def _recheck_batch(
        self, proxy_ids: list[UUID], checker: ProxyChecker, *, actor: str, manual: bool
    ) -> dict[UUID, str]:
        """Проверить прокси параллельно и применить исходы.

        Сбой чекера и одновременный отказ всех прокси группы не считаются отказом прокси: это
        почти всегда недоступность контрольного адреса или провайдера целиком (R2-F04). Такие
        прокси остаются как есть и проверяются позже; пишется событие `proxy.checker.failed`.
        """
        rows = [await self._get_proxy(proxy_id) for proxy_id in proxy_ids]
        rows = [
            row for row in rows if row.status != ProxyStatus.ALIVE and (manual or not self._is_pinned(row))
        ]
        if not rows:
            return {}
        slots = asyncio.Semaphore(max(1, self._proxy_health.check_concurrency))

        async def probe(row: ProxyRow) -> bool | None:
            async with slots:
                return await self._check(row, checker)

        outcomes = await asyncio.gather(*(probe(row) for row in rows))
        by_group: dict[str, list[bool | None]] = {}
        for row, outcome in zip(rows, outcomes, strict=True):
            by_group.setdefault(row.pool, []).append(outcome)
        # отказ всех при двух и более проверенных — подозрение на общий сбой, а не на каждый прокси
        outage = {
            group
            for group, results in by_group.items()
            if len(results) >= 2 and all(result is False for result in results)
        }
        statuses: dict[UUID, str] = {}
        deferred: dict[tuple[str, str], int] = {}
        for row, outcome in zip(rows, outcomes, strict=True):
            if outcome is None or row.pool in outage:
                reason = "checker_error" if outcome is None else "all_failed"
                deferred[(row.pool, reason)] = deferred.get((row.pool, reason), 0) + 1
                statuses[row.id] = await self._defer_check(row)
            else:
                statuses[row.id] = await self._apply_check(row, outcome, actor=actor, manual=manual)
        for (group, reason), count in deferred.items():
            await self._checker_failed(group, reason, count, actor)
        return statuses

    def _is_pinned(self, row: ProxyRow) -> bool:
        """Статус закреплён вручную. По часам хаба — быстрый отсев; точная проверка — в транзакции."""
        return row.pinned_until is not None and row.pinned_until > self.hub.clock.now()

    async def _check(self, row: ProxyRow, checker: ProxyChecker) -> bool | None:
        """Один вызов чекера: `True`/`False` — ответ о прокси, `None` — чекер упал сам."""
        url = self._proxy_url_of(row)
        try:
            return bool(await asyncio.wait_for(checker(url), timeout=self._proxy_health.check_timeout))
        except TimeoutError:
            return False  # прокси не ответил за отведённое время
        except Exception as exc:
            # без traceback: в тексте исключения сетевого клиента бывает URL с паролем прокси
            logger.warning("чекер прокси %s упал: %s", row.id, describe_error(exc))
            return None

    async def _defer_check(self, row: ProxyRow) -> str:
        """Отложить перепроверку, не засчитывая отказ."""
        policy = self._proxy_health
        async with self._tx(own=True) as tx:
            current = await self._proxy(tx, row.id)
            now = await tx.now()
            pause = policy.dead_delay() if current.status == ProxyStatus.DEAD else policy.delay(0)
            await tx.update_proxy(row.id, {"checked_at": now, "next_check_at": now + pause})
            return current.status

    async def _checker_failed(self, group: str, reason: str, count: int, actor: str) -> None:
        try:
            async with self._tx(own=True) as tx:
                await self._emit(
                    tx,
                    EventDraft(
                        "proxy.checker.failed",
                        actor=actor,
                        data={"pool": group, "reason": reason, "proxies": count},
                    ),
                )
        except Exception as exc:
            logger.warning("не удалось записать proxy.checker.failed: %s", describe_error(exc))

    async def _recheck_proxy(self, proxy_id: UUID, checker: ProxyChecker, *, actor: str) -> str:
        """Ручная перепроверка: до двух проверок подряд — выход из карантина или `dead` подтверждается."""
        status = await self._recheck_once(proxy_id, checker, actor=actor, manual=True)
        row = await self._get_proxy(proxy_id)
        if row.pending_observation == ProxyStatus.ALIVE:
            status = await self._recheck_once(proxy_id, checker, actor=actor, manual=True)
        return status

    async def _recheck_once(
        self, proxy_id: UUID, checker: ProxyChecker, *, actor: str, manual: bool = False
    ) -> str:
        row = await self._get_proxy(proxy_id)
        if row.status == ProxyStatus.ALIVE or (not manual and self._is_pinned(row)):
            return row.status
        outcome = await self._check(row, checker)
        if outcome is None:
            return await self._defer_check(row)
        return await self._apply_check(row, outcome, actor=actor, manual=manual)

    async def _apply_check(self, row: ProxyRow, ok: bool, *, actor: str, manual: bool) -> str:
        policy = self._proxy_health
        try:
            async with self._tx(own=True) as tx:
                current = await self._proxy(tx, row.id)
                if current.status != row.status:
                    return current.status  # изменился параллельно
                now = await tx.now()
                if not manual and current.pinned_until is not None and current.pinned_until > now:
                    return current.status  # закрепление защищает от автоматических переходов (R2-F14)
                if ok:
                    extra: dict[str, Any] = {"checked_at": now, "next_check_at": now + policy.base_backoff}
                    decision = await self._apply(
                        tx, current, Observation(ProxyStatus.ALIVE, observed_at=now, actor=actor), extra
                    )
                    if decision.state.state == ProxyStatus.ALIVE:
                        await tx.update_proxy(
                            row.id, {"fail_streak": 0, "error_score": 0.0, "next_check_at": None}
                        )
                    return decision.state.state
                streak = current.fail_streak + 1
                extra = {"checked_at": now, "fail_streak": streak}
                if current.status == ProxyStatus.QUARANTINED and streak >= policy.dead_after:
                    # dead не навсегда: редкая автоматическая перепроверка вернёт ожившую группу (R2-F04)
                    extra["next_check_at"] = now + policy.dead_delay()
                    change: Change = Command(
                        ProxyStatus.DEAD, actor=actor, reason=f"{streak} неудачных перепроверок"
                    )
                else:
                    pause = (
                        policy.dead_delay() if current.status == ProxyStatus.DEAD else policy.delay(streak)
                    )
                    extra["next_check_at"] = now + pause
                    change = Observation(
                        current.status, observed_at=now, actor=actor
                    )  # сбросить ожидание выхода
                return (await self._apply(tx, current, change, extra)).state.state
        except ConcurrentModification:
            return (await self._get_proxy(row.id)).status

    async def _override_proxy(
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
                extra = {"next_check_at": await tx.now() + self._proxy_health.base_backoff}
            change = machine.Override(to=str(status), reason=reason, actor=actor, pin_for=pin_for)
            return (await self._apply(tx, row, change, extra)).state.state

    async def _delete_proxy(self, proxy_id: UUID, *, actor: str, unbind: bool = False) -> int:
        """Удалить прокси; с привязанными аккаунтами — только при unbind=True.

        Возвращает число отвязанных аккаунтов.
        """
        async with self._tx() as tx:
            row = await self._proxy(tx, proxy_id)
            bound = await tx.proxy_bound_identities(proxy_id)
            if bound and not unbind:
                raise ValidationFailed(
                    f"к прокси привязано аккаунтов: {len(bound)}; перепривяжите их или удалите с unbind=True"
                )
            services = {item.identity_id: item.service for item in bound}
            unbound = await tx.delete_proxy(proxy_id) or []
            drafts = [
                EventDraft(
                    "proxy.binding.changed",
                    actor=actor,
                    service=services.get(identity_id),
                    identity_id=identity_id,
                    data={
                        "from_proxy_id": str(proxy_id),
                        "to_proxy_id": None,
                        "reason": "proxy deleted",
                        "pool": row.pool,
                    },
                )
                for identity_id in unbound
            ]
            drafts.append(
                EventDraft(
                    "proxy.deleted",
                    actor=actor,
                    data={
                        "proxy_id": str(proxy_id),
                        "pool": row.pool,
                        "host": row.host,
                        "port": row.port,
                        "unbound": len(unbound),
                    },
                )
            )
            await self._emit(tx, *drafts)
            return len(unbound)

    async def _replace_proxy_credentials(
        self, proxy_id: UUID, *, username: str | None, password: str | None, actor: str
    ) -> None:
        """Сменить логин и пароль прокси; адрес и привязки остаются."""
        if password and not username:
            raise ValidationFailed("прокси: пароль без логина не задаётся")
        secret = ProxySecret(username=username, password=SecretStr(password) if password else None)
        payload = self.hub._envelope.seal_model(secret, associated_data("ih_proxies", proxy_id))
        async with self._tx() as tx:
            row = await self._proxy(tx, proxy_id)
            tag, key_id, aliases = self._proxy_fingerprints(row.scheme, row.host, row.port, username)
            other = await tx.proxy_by_fingerprint([tag, *aliases])
            if other is not None and other != proxy_id:
                raise AlreadyExists("proxy", f"{row.scheme}://{row.host}:{row.port}")
            await tx.set_proxy_secret(proxy_id, tag, payload, fingerprint_key_id=key_id)
            await self._proxy_updated(tx, row, actor, ["credentials"])

    async def _move_proxy(self, proxy_id: UUID, *, group: str, actor: str) -> None:
        """Перенести прокси в другую группу; привязки аккаунтов сохраняются."""
        if not group or len(group) > 128:
            raise ValidationFailed("пул прокси: от 1 до 128 символов")
        async with self._tx() as tx:
            row = await self._proxy(tx, proxy_id)
            if row.pool == group:
                return
            await tx.update_proxy(proxy_id, {"pool": group})
            await self._proxy_updated(tx, replace(row, pool=group), actor, ["group"], previous=row.pool)

    async def _proxy_updated(
        self, tx: StoreTx, row: ProxyRow, actor: str, fields: list[str], *, previous: str | None = None
    ) -> None:
        data: dict[str, Any] = {"fields": fields, "pool": row.pool, "host": row.host, "port": row.port}
        if previous is not None:
            data["from_pool"] = previous
        await self._emit(tx, EventDraft("proxy.updated", actor=actor, proxy_id=row.id, data=data))

    async def _rebind_proxy(self, identity_id: UUID, proxy_id: UUID, *, reason: str, actor: str) -> bool:
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
                raise LeaseUnavailable(
                    "target proxy has no available capacity", reason=LeaseUnavailableReason.NO_PROXY
                )
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
