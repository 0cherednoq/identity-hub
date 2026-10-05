"""`space.verifications` — факторы верификации и требования платформы вне аренды (IMP-23).

Процесс проверки (start → submit → решение) идёт под арендой через `lease.verification(factor)`;
здесь — чтение статуса, решение человека и учёт требований платформы.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from identity_hub.api._base import Namespace
from identity_hub.domain.verification import VerificationStatus

__all__ = ["Requirements", "Verifications"]


class Requirements(Namespace):
    """Что платформа требует от identity сейчас: `space.verifications.requirements`.

    Требования видны в read-моделях и событиях `identity.requirement.*`, но на допуск к
    операциям не влияют.
    """

    __slots__ = ()

    async def add(
        self,
        identity_id: UUID,
        code: str,
        *,
        deadline: datetime | None = None,
        actor: str,
        expected_revision: int | None = None,
    ) -> bool:
        """Добавить требование; `False` — такое уже есть."""
        space = self._at(identity_id, expected_revision)
        return await space._add_requirement(identity_id, code, deadline=deadline, actor=actor)

    async def resolve(
        self,
        identity_id: UUID,
        code: str,
        *,
        actor: str,
        reason: str | None = None,
        expected_revision: int | None = None,
    ) -> bool:
        """Снять требование; `False` — его не было."""
        space = self._at(identity_id, expected_revision)
        return await space._resolve_requirement(identity_id, code, actor=actor, reason=reason)


class Verifications(Namespace):
    """Верификация identity: `space.verifications`."""

    __slots__ = ()

    @property
    def requirements(self) -> Requirements:
        """Требования платформы: `add`, `resolve`."""
        return Requirements(self._space)

    async def status(self, identity_id: UUID, factor: str) -> VerificationStatus:
        """Состояние фактора, число попыток и срок действия."""
        return await self._space._verification_status(identity_id, factor)

    async def review(
        self,
        identity_id: UUID,
        factor: str,
        action: Literal["approve", "reject", "abandon", "reset_attempts"],
        *,
        actor: str,
        reason: str,
        code: str | None = None,
        expected_revision: int | None = None,
    ) -> VerificationStatus:
        """Решение человека: `approve` → verified, `reject` (нужен `code`) → rejected, `abandon` → missing."""
        return await self._at(identity_id, expected_revision)._review_verification(
            identity_id, factor, action, actor=actor, reason=reason, code=code
        )
