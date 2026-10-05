"""`space.links` — связи между identity одного пространства (IMP-23).

Связь `identity —role→ linked` означает «identity зависит от linked» (почта для восстановления,
телефон для входа). Связанный identity доступен в `Authenticator.login` через `ctx.linked(role)`.
"""

from __future__ import annotations

from uuid import UUID

from identity_hub.api._base import Namespace
from identity_hub.links import Impact, Links

__all__ = ["IdentityLinks"]


class IdentityLinks(Namespace):
    """Связи identity: `space.links`."""

    __slots__ = ()

    async def add(
        self,
        identity_id: UUID,
        role: str,
        linked_identity_id: UUID,
        *,
        actor: str,
        reason: str | None = None,
        expected_revision: int | None = None,
    ) -> None:
        """Добавить связь; на пару (identity, role) — одна связь, повтор — `AlreadyExists`.

        `expected_revision` сверяется у `identity_id` — зависимого аккаунта.
        """
        await self._at(identity_id, expected_revision)._link(
            identity_id, role, linked_identity_id, actor=actor, reason=reason
        )

    async def remove(
        self,
        identity_id: UUID,
        role: str,
        *,
        actor: str,
        reason: str | None = None,
        expected_revision: int | None = None,
    ) -> bool:
        """Удалить связь; `False` — её не было."""
        return await self._at(identity_id, expected_revision)._unlink(
            identity_id, role, actor=actor, reason=reason
        )

    async def of(self, identity_id: UUID) -> Links:
        """Исходящие (от кого зависит identity) и входящие (кто зависит от него) связи."""
        return await self._space._links_of(identity_id)

    async def impact(self, identity_id: UUID, max_depth: int = 5) -> list[Impact]:
        """Кого затронет потеря identity: транзитивно зависящие от него identity."""
        return await self._space._impact_of(identity_id, max_depth)
