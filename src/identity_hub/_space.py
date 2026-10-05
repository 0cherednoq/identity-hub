"""База пространства: транзакции и события. Операции — в миксинах (state, identities, …)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
from functools import cached_property
from typing import TYPE_CHECKING, Any, Self, cast
from uuid import UUID

from identity_hub.domain.data import validate_json_payload
from identity_hub.domain.redaction import REDACTED
from identity_hub.errors import NotFound, ValidationFailed
from identity_hub.events import EventDraft, scrub
from identity_hub.store.base import IdentityRow, SpaceRef, StoreTx

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from identity_hub.dispatch.notifications import Notifications
    from identity_hub.dispatch.webhooks import Webhooks
    from identity_hub.hub import IdentityHub, SpaceHandle

__all__ = ["SpaceBase", "logger", "require_actor_and_reason"]

logger = logging.getLogger("identity_hub")

# поля событий с идентификаторами людей: телефон, email, произвольные метки (R2-S17)
_REF_FIELDS = ("external_ref", "normalized_ref", "labels")


def _without_refs(data: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: (REDACTED if key in _REF_FIELDS and value is not None else value) for key, value in data.items()
    }


def require_actor_and_reason(actor: object, reason: object, action: str) -> tuple[str, str]:
    """Автор и причина аудируемой команды без краевых пробелов; пустые — `ValidationFailed`."""
    if not isinstance(actor, str) or not actor.strip():
        raise ValidationFailed(f"{action} требует автора")
    if not isinstance(reason, str) or not reason.strip():
        raise ValidationFailed(f"{action} требует причины")
    return actor.strip(), reason.strip()


class SpaceBase:
    def __init__(
        self,
        hub: IdentityHub,
        slug: str,
        *,
        title: str | None = None,
        space_id: UUID | None = None,
        session: AsyncSession | None = None,
        create: bool = False,
    ) -> None:
        self.hub = hub
        self.slug = slug
        self._title = title
        self._space_id = space_id
        self._session = session
        self._create = create  # создать пространство при первом обращении, если его нет
        # (identity, ревизия), которую сверяет каждая транзакция команды (IMP-07, D-206)
        self._expected: tuple[UUID, int] | None = None

    def using(self, session: AsyncSession) -> Self:
        """Операции общей группы (ТЗ §7.2) выполняются в транзакции пользователя (savepoint)."""
        handle = self._copy()
        handle._session = session
        return handle

    def _expecting(self, identity_id: UUID, expected_revision: int | None) -> Self:
        """Копия хэндла, чьи транзакции сверяют ревизию identity; `None` — без проверки.

        Проверка — первая блокировка транзакции (`lock_identity_revision`, `FOR UPDATE` строки
        identity): параллельная команда с той же ревизией ждёт коммита и получает
        `RevisionConflict`. Её повторяет каждая транзакция команды, в том числе вспомогательные
        чтения до шифрования: ревизия между ними не меняется, а конфликт виден раньше. Своя
        транзакция (`own=True`) при `using(session)` проверку не делает: строку может держать
        транзакция пользователя. Команда, которая пишет в своей транзакции, вызывает
        `_check_expected` сама.
        """
        if expected_revision is None:
            return self
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise ValidationFailed("expected_revision должен быть целым числом")
        if expected_revision < 0:
            raise ValidationFailed("expected_revision не может быть отрицательным")
        handle = self._copy()
        handle._expected = (identity_id, expected_revision)
        return handle

    def _copy(self) -> Self:
        return type(self)(
            self.hub,
            self.slug,
            title=self._title,
            space_id=self._space_id,
            session=self._session,
            create=self._create,
        )

    async def ref(self) -> SpaceRef:
        """Строка пространства; без `create=True` несуществующее пространство — `NotFound` (R1-07)."""
        return await self.hub._space_ref(self.slug, self._title, self._space_id, create=self._create)

    @cached_property
    def webhooks(self) -> Webhooks:
        """Управление исходящими webhook endpoint'ами пространства (FR-14)."""
        from identity_hub.dispatch.webhooks import Webhooks

        return Webhooks(cast("SpaceHandle", self))

    @cached_property
    def notifications(self) -> Notifications:
        """Управление Telegram-каналами и правилами пространства (FR-15)."""
        from identity_hub.dispatch.notifications import Notifications

        return Notifications(cast("SpaceHandle", self))

    @asynccontextmanager
    async def _tx(self, *, own: bool = False, checked: bool = True) -> AsyncIterator[StoreTx]:
        """Транзакция операции. `own=True` — всегда своя (факты внешнего мира, ТЗ §7.2).

        `checked=False` — сверку `expected_revision` команда делает сама, после ключа
        идемпотентности: он блокируется раньше строки identity (порядок блокировок, п. 0).
        """
        ref = await self.ref()
        session: Any = None if own else self._session
        async with self.hub._store.transaction(ref, session=session) as tx:
            if checked and not (own and self._session is not None):
                await self._check_expected(tx)
            yield tx

    async def _check_expected(self, tx: StoreTx) -> None:
        if self._expected is not None:
            await tx.lock_identity_revision(*self._expected)

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
