"""Связи аккаунт → аккаунт для входа и рекавери (ТЗ FR-4)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from identity_hub._space import SpaceBase
from identity_hub.errors import AlreadyExists, NotFound, ValidationFailed
from identity_hub.events import EventDraft

__all__ = ["Impact", "Link", "LinkSummary", "Links", "LinksMixin"]


@dataclass(frozen=True, slots=True)
class Link:
    role: str
    identity_id: UUID  # противоположная сторона
    service: str
    external_ref: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Links:
    """`outgoing` — от кого зависит аккаунт; `incoming` — кто зависит от него."""

    outgoing: list[Link]
    incoming: list[Link]

    def linked(self, role: str) -> Link | None:
        return next((link for link in self.outgoing if link.role == role), None)


@dataclass(frozen=True, slots=True)
class LinkSummary:
    outgoing: int
    incoming: int
    roles: list[str]


@dataclass(frozen=True, slots=True)
class Impact:
    """Аккаунт, затронутый потерей исходного (транзитивно), и цепочка ролей до него."""

    identity_id: UUID
    depth: int
    path: list[str]


class LinksMixin(SpaceBase):
    async def _link(
        self,
        identity_id: UUID,
        role: str,
        linked_identity_id: UUID,
        *,
        actor: str = "system",
        reason: str | None = None,
    ) -> None:
        """Связь `identity_id --role--> linked_identity_id` (FR-4.1, 4.2). Одна связь на (аккаунт, роль)."""
        if not role or len(role) > 64:
            raise ValidationFailed("роль связи: от 1 до 64 символов")
        if identity_id == linked_identity_id:
            raise ValidationFailed("связь аккаунта с самим собой запрещена (FR-4.2)")
        async with self._tx() as tx:
            row = await self._identity(tx, identity_id)
            if linked_identity_id not in await tx.existing_identities([linked_identity_id]):
                raise NotFound("identity", linked_identity_id)
            if not await tx.insert_link(identity_id, role, linked_identity_id):
                raise AlreadyExists("link", f"{identity_id}/{role}")
            await self._emit(
                tx,
                EventDraft(
                    "identity.linked",
                    actor=actor,
                    service=row.service,
                    identity_id=identity_id,
                    data={
                        "role": role,
                        "linked_identity_id": str(linked_identity_id),
                        **({"reason": reason} if reason else {}),
                    },
                ),
            )

    async def _unlink(
        self, identity_id: UUID, role: str, *, actor: str = "system", reason: str | None = None
    ) -> bool:
        """Удалить связь; False — её не было."""
        async with self._tx() as tx:
            row = await self._identity(tx, identity_id)
            linked = await tx.delete_link(identity_id, role)
            if linked is None:
                return False
            await self._emit(
                tx,
                EventDraft(
                    "identity.unlinked",
                    actor=actor,
                    service=row.service,
                    identity_id=identity_id,
                    data={
                        "role": role,
                        "linked_identity_id": str(linked),
                        **({"reason": reason} if reason else {}),
                    },
                ),
            )
            return True

    async def _links_of(self, identity_id: UUID) -> Links:
        """Связи в обе стороны (FR-4.4)."""
        async with self._tx() as tx:
            await self._identity(tx, identity_id)
            outgoing, incoming = await tx.links_of(identity_id)
        return Links(
            outgoing=[
                Link(r.role, r.linked_identity_id, r.other_service, r.other_external_ref, r.created_at)
                for r in outgoing
            ],
            incoming=[
                Link(r.role, r.identity_id, r.other_service, r.other_external_ref, r.created_at)
                for r in incoming
            ],
        )

    async def _link_summary(self, ids: Sequence[UUID]) -> dict[UUID, LinkSummary]:
        """Сводка по странице списка одним запросом (FR-4.4)."""
        async with self._tx() as tx:
            counts = await tx.link_counts(ids)
        return {i: LinkSummary(*counts.get(i, (0, 0, []))) for i in ids}

    async def _impact_of(self, identity_id: UUID, max_depth: int = 5) -> list[Impact]:
        """Кого затронет потеря аккаунта: транзитивно зависящие аккаунты (FR-4.4)."""
        if max_depth < 1:
            raise ValidationFailed("max_depth ≥ 1")
        async with self._tx() as tx:
            await self._identity(tx, identity_id)
            rows = await tx.dependents(identity_id, max_depth)
        return [Impact(i, depth, path) for i, depth, path in rows]
