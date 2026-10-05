"""`space.secrets` — секреты identity вне аренды: метаданные, замена и раскрытие с аудитом."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID

from pydantic import BaseModel

from identity_hub.api._base import Namespace
from identity_hub.domain.credentials import ACTIVE, DISABLED, REVOKED, is_expired
from identity_hub.errors import ValidationFailed

if TYPE_CHECKING:
    from identity_hub.read.cards import AccessCard

__all__ = ["Credential", "SecretRef", "Secrets"]


@dataclass(frozen=True, slots=True)
class Credential:
    """Запись credentials без секрета (IMP-09): вид, имя, открытая часть, жизненный цикл, срок."""

    id: UUID
    kind: str
    name: str
    public_value: dict[str, Any] | None
    lifecycle: str
    expires_at: datetime | None
    expired: bool  # срок вышел — считается по времени, не переходом (D-215)
    revision: int
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: Any, now: datetime) -> Credential:
        return cls(
            id=row.id,
            kind=row.kind,
            name=row.name,
            public_value=row.public_value,
            lifecycle=row.lifecycle,
            expires_at=row.expires_at,
            expired=is_expired(row.lifecycle, row.expires_at, now),
            revision=row.revision,
            version=row.version,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


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
        self, identity_id: UUID, secret: SecretRef | str, *, actor: str, reason: str
    ) -> dict[str, Any]:
        """Раскрыть секрет; событие `identity.secret.revealed` пишется в той же транзакции.

        `actor` и `reason` обязательны и непусты: раскрытие без автора и причины не аудируется.
        """
        return await self._space._reveal_secret(
            identity_id, str(SecretRef.parse(secret)), actor=actor, reason=reason
        )

    async def replace_credentials(
        self,
        identity_id: UUID,
        credentials: BaseModel | Mapping[str, Any],
        *,
        actor: str,
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        """Заменить credentials целиком (валидация схемой сервиса, шифрование до транзакции).

        `idempotency_key` — повтор с теми же данными ничего не пишет, с другими —
        `IdempotencyConflict`; секрет в ключ не попадает, только его HMAC-тег (D-208).
        """
        space = self._at(identity_id, expected_revision)
        await space._update_credentials(
            identity_id, credentials, actor=actor, idempotency_key=idempotency_key
        )

    async def replace_session(
        self,
        identity_id: UUID,
        session: BaseModel | Mapping[str, Any],
        *,
        actor: str,
        slot: str = "default",
        expires_at: datetime | None = None,
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        """Заменить сессию слота вне аренды на текущей границе fencing; под арендой — `lease.save_session`.

        `idempotency_key` — как у `replace_credentials` (D-208).
        """
        await self._at(identity_id, expected_revision)._update_session(
            identity_id,
            session,
            actor=actor,
            slot=slot,
            expires_at=expires_at,
            idempotency_key=idempotency_key,
        )

    # ------------------------------------------------------------------ несколько credentials (IMP-09)

    async def put_credential(
        self,
        identity_id: UUID,
        kind: str,
        value: BaseModel | Mapping[str, Any],
        *,
        name: str = "default",
        public_value: Mapping[str, Any] | None = None,
        expires_at: datetime | None = None,
        actor: str,
        expected_revision: int | None = None,
    ) -> Credential:
        """Создать или заменить запись (kind, name): секрет по схеме `Service.credential_schema(kind)`,
        открытая часть и срок. Отозванную запись заменить нельзя — `TransitionNotAllowed` (D-215)."""
        space = self._at(identity_id, expected_revision)
        row = await space._put_credential(
            identity_id, kind, value, name=name, public_value=public_value, expires_at=expires_at, actor=actor
        )
        return Credential.from_row(row, row.updated_at)

    async def credentials(self, identity_id: UUID) -> list[Credential]:
        """Записи credentials аккаунта без секретов, по (kind, name)."""
        rows, now = await self._space._credential_list(identity_id)
        return [Credential.from_row(row, now) for row in rows]

    async def disable_credential(
        self,
        identity_id: UUID,
        kind: str,
        name: str = "default",
        *,
        actor: str,
        expected_revision: int | None = None,
    ) -> bool:
        """`active → disabled`; `False` — уже выключена."""
        space = self._at(identity_id, expected_revision)
        return await space._set_credential_lifecycle(identity_id, kind, name, DISABLED, actor=actor)

    async def enable_credential(
        self,
        identity_id: UUID,
        kind: str,
        name: str = "default",
        *,
        actor: str,
        expected_revision: int | None = None,
    ) -> bool:
        """`disabled → active`; `False` — уже включена; отозванную — `TransitionNotAllowed`."""
        space = self._at(identity_id, expected_revision)
        return await space._set_credential_lifecycle(identity_id, kind, name, ACTIVE, actor=actor)

    async def revoke_credential(
        self,
        identity_id: UUID,
        kind: str,
        name: str = "default",
        *,
        actor: str,
        reason: str | None = None,
        expected_revision: int | None = None,
    ) -> bool:
        """Отозвать навсегда (`revoked` — конечное); `False` — уже отозвана."""
        space = self._at(identity_id, expected_revision)
        return await space._set_credential_lifecycle(
            identity_id, kind, name, REVOKED, actor=actor, reason=reason
        )
