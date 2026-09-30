"""`space.secrets` — секреты identity вне аренды: метаданные, замена и раскрытие с аудитом."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID

from pydantic import BaseModel

from identity_hub.api._base import Namespace
from identity_hub.errors import ValidationFailed

if TYPE_CHECKING:
    from identity_hub.read.cards import AccessCard

__all__ = ["SecretRef", "Secrets"]


@dataclass(frozen=True, slots=True)
class SecretRef:
    """Какой секрет раскрыть: `SecretRef.CREDENTIALS`, `SecretRef.session("web")`,
    `SecretRef.PROFILE`, `SecretRef.evidence("kyc")`."""

    kind: str
    key: str = ""

    CREDENTIALS: ClassVar[SecretRef]
    PROFILE: ClassVar[SecretRef]

    @classmethod
    def session(cls, slot: str = "default") -> SecretRef:
        return cls("session", slot)

    @classmethod
    def evidence(cls, factor: str) -> SecretRef:
        if not factor:
            raise ValidationFailed("evidence: нужен фактор верификации")
        return cls("evidence", factor)

    @classmethod
    def parse(cls, value: SecretRef | str) -> SecretRef:
        """Строковая форма `credentials`, `session[:slot]`, `profile`, `evidence:<factor>`."""
        if isinstance(value, SecretRef):
            return value
        kind, _, key = value.partition(":")
        return cls(kind, key)

    def __str__(self) -> str:
        return f"{self.kind}:{self.key}" if self.key else self.kind


SecretRef.CREDENTIALS = SecretRef("credentials")
SecretRef.PROFILE = SecretRef("profile")


class Secrets(Namespace):
    """Секреты identity: `space.secrets`. Под арендой — `lease.credentials()` / `lease.session()`."""

    __slots__ = ()

    async def describe(self, identity_id: UUID) -> AccessCard:
        """Метаданные credentials и сессий (есть ли, версия, срок, статус) — без значений."""
        return await self._space._access_card(identity_id)

    async def reveal(
        self, identity_id: UUID, secret: SecretRef | str, *, actor: str, reason: str | None = None
    ) -> dict[str, Any]:
        """Раскрыть секрет; событие `identity.secret.revealed` пишется в той же транзакции."""
        return await self._space._reveal_secret(
            identity_id, str(SecretRef.parse(secret)), actor=actor, reason=reason
        )

    async def replace_credentials(
        self, identity_id: UUID, credentials: BaseModel | Mapping[str, Any], *, actor: str
    ) -> None:
        """Заменить credentials целиком (валидация схемой сервиса, шифрование до транзакции)."""
        await self._space._update_credentials(identity_id, credentials, actor=actor)

    async def replace_session(
        self,
        identity_id: UUID,
        session: BaseModel | Mapping[str, Any],
        *,
        actor: str,
        slot: str = "default",
        expires_at: datetime | None = None,
    ) -> None:
        """Заменить сессию слота вне аренды на текущей границе fencing; под арендой — `lease.save_session`."""
        await self._space._update_session(identity_id, session, actor=actor, slot=slot, expires_at=expires_at)
