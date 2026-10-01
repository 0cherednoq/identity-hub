"""База пространства: транзакции и события. Операции — в миксинах (state, identities, …)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Self
from uuid import UUID

from identity_hub.domain.data import validate_json_payload
from identity_hub.domain.redaction import REDACTED
from identity_hub.errors import NotFound
from identity_hub.events import EventDraft, scrub
from identity_hub.store.base import IdentityRow, SpaceRef, StoreTx

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from identity_hub.dispatch.notifications import Notifications
    from identity_hub.dispatch.webhooks import Webhooks
    from identity_hub.hub import IdentityHub

__all__ = ["SpaceBase", "logger"]

logger = logging.getLogger("identity_hub")

# поля событий с идентификаторами людей: телефон, email, произвольные метки (R2-S17)
_REF_FIELDS = ("external_ref", "normalized_ref", "labels")


def _without_refs(data: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: (REDACTED if key in _REF_FIELDS and value is not None else value) for key, value in data.items()
    }


class SpaceBase:
    def __init__(
        self,
        hub: IdentityHub,
        slug: str,
        *,
        title: str | None = None,
        space_id: UUID | None = None,
        session: AsyncSession | None = None,
    ) -> None:
        self.hub = hub
        self.slug = slug
        self._title = title
        self._space_id = space_id
        self._session = session

    def using(self, session: AsyncSession) -> Self:
        """Операции общей группы (ТЗ §7.2) выполняются в транзакции пользователя (savepoint)."""
        return type(self)(self.hub, self.slug, title=self._title, space_id=self._space_id, session=session)

    async def ref(self) -> SpaceRef:
        return await self.hub._space_ref(self.slug, self._title, self._space_id)

    @property
    def webhooks(self) -> Webhooks:
        """Управление исходящими webhook endpoint'ами пространства (FR-14)."""
        from identity_hub.dispatch.webhooks import Webhooks

        return Webhooks(self)

    @property
    def notifications(self) -> Notifications:
        """Управление Telegram-каналами и правилами пространства (FR-15)."""
        from identity_hub.dispatch.notifications import Notifications

        return Notifications(self)

    @asynccontextmanager
    async def _tx(self, *, own: bool = False) -> AsyncIterator[StoreTx]:
        """Транзакция операции. `own=True` — всегда своя (факты внешнего мира, ТЗ §7.2)."""
        ref = await self.ref()
        session: Any = None if own else self._session
        async with self.hub.store.transaction(ref, session=session) as tx:
            yield tx

    async def _emit(self, tx: StoreTx, *drafts: EventDraft) -> list[UUID]:
        """Записать события в транзакции операции; данные очищаются от секретов (S-8)."""
        strict = self.hub.strict_events
        clean = [replace(d, data=scrub(d.data, strict=strict)) for d in drafts]
        if self.hub.redact_refs_in_events:
            clean = [replace(d, data=_without_refs(d.data)) for d in clean]
        for draft in clean:
            validate_json_payload(draft.data, f"событие {draft.type}")
        return await tx.emit(clean)

    @staticmethod
    async def _identity(tx: StoreTx, identity_id: UUID) -> IdentityRow:
        row = await tx.get_identity(identity_id)
        if row is None:
            raise NotFound("identity", identity_id)
        return row

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.slug!r}{', using session' if self._session else ''})"
