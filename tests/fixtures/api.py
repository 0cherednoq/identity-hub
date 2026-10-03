"""Короткие помощники тестов поверх нового API (IMP-23, I8.8b).

Тесты создают identity и прокси сотнями; эти обёртки сохраняют форму результата, которой
пользуются проверки (`identity_id`/`created`, `(proxy_id, created)`), но вызывают только
неймспейсы `space.identities` и `space.proxies`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from identity_hub import IdentityCreate, SpaceHandle

__all__ = ["Registered", "add_proxy", "register"]


@dataclass(frozen=True, slots=True)
class Registered:
    identity_id: UUID
    created: bool


async def register(space: SpaceHandle, *, actor: str = "system", **fields: Any) -> Registered:
    """`space.identities.ensure(IdentityCreate(**fields), actor=actor)`: get-or-create без изменений."""
    identity, created = await space.identities.ensure(IdentityCreate(**fields), actor=actor)
    return Registered(identity.id, created)


async def add_proxy(
    space: SpaceHandle, url: str | None = None, *, pool: str, **fields: Any
) -> tuple[UUID, bool]:
    """`space.proxies.ensure(url, group=pool)`: id прокси и признак создания."""
    proxy, created = await space.proxies.ensure(url, group=pool, **fields)
    return proxy.id, created
