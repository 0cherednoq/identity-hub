"""`space.pools` — пулы аренды: правила отбора identity и выдача их воркерам (IMP-23)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import timedelta
from typing import Any
from uuid import UUID

from identity_hub.api._base import Namespace
from identity_hub.pool import Pool
from identity_hub.service import Pacing, ProxyPolicy, Service

__all__ = ["Pools"]


class Pools(Namespace):
    """Пулы аренды: `space.pools`."""

    __slots__ = ()

    def define(
        self,
        name: str,
        *,
        service: Service | str,
        tags: Iterable[str] = (),
        labels: Mapping[str, Any] | None = None,
        profile: Mapping[str, Any] | None = None,
        pacing: Pacing | None = None,
        proxy_policy: ProxyPolicy | None = None,
        ttl: timedelta | None = None,
        heartbeat: timedelta | None = None,
        safety_margin: timedelta | None = None,
        holder: str | None = None,
        capacity_low: int | None = None,
        stats_flush: timedelta | None = None,
        purpose: str | None = None,
        max_hold: timedelta | None = None,
        reserve: int = 0,
        reserve_strict: bool = False,
        priority: int = 0,
        yield_grace: timedelta | None = None,
    ) -> Pool:
        """Объявить пул: фильтры отбора и политики аренды. В БД не хранится, создаётся дёшево.

        `purpose` — назначение аренд пула по умолчанию: метка вида работы для истории и сводок.
        `max_hold` — потолок удержания аренды: зависшая задача не держит аккаунт дольше.

        Сама выборка кандидата происходит при `pool.lease()` / `pool.acquire()`.
        """
        return self._space._pool(
            name,
            service=service,
            tags=tags,
            labels=labels,
            profile=profile,
            pacing=pacing,
            proxy_policy=proxy_policy,
            ttl=ttl,
            heartbeat=heartbeat,
            safety_margin=safety_margin,
            holder=holder,
            capacity_low=capacity_low,
            stats_flush=stats_flush,
            purpose=purpose,
            max_hold=max_hold,
            reserve=reserve,
            reserve_strict=reserve_strict,
            priority=priority,
            yield_grace=yield_grace,
        )

    async def revoke(self, identity_id: UUID, *, reason: str, actor: str) -> bool:
        """Отозвать живую аренду identity: воркер потеряет её на ближайшем heartbeat.

        `False` — живой аренды не было.
        """
        return await self._space._revoke_lease(identity_id, reason=reason, actor=actor)
