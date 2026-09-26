"""Секреты из админки (ТЗ FR-5.2, S-7): обновление credentials и раскрытие с аудитом.

Листинги, поиск и read-модели секретов не содержат; единственный путь увидеть секрет вне аренды —
`reveal_secret`, который пишет событие `identity.secret.revealed` (кто, когда, какой аккаунт, какое поле).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from identity_hub._space import SpaceBase
from identity_hub.crypto import associated_data, reveal
from identity_hub.domain.data import validate_model
from identity_hub.errors import NotFound, ValidationFailed
from identity_hub.events import EventDraft

__all__ = ["SecretsAdminMixin"]


class SecretsAdminMixin(SpaceBase):
    async def update_credentials(
        self, identity_id: UUID, credentials: BaseModel | Mapping[str, Any], *, actor: str
    ) -> None:
        """Заменить credentials (валидация схемой сервиса, шифрование до транзакции)."""
        async with self._tx(own=True) as tx:
            service = self.hub.service((await self._identity(tx, identity_id)).service)
        model = validate_model(service.credentials, credentials, "credentials")
        payload = self.hub.envelope.seal_model(model, associated_data("ih_credentials", identity_id))
        async with self._tx() as tx:
            row = await self._identity(tx, identity_id)
            await tx.put_credentials(identity_id, payload)
            await self._emit(
                tx,
                EventDraft(
                    "identity.credentials.updated",
                    actor=actor,
                    service=row.service,
                    identity_id=identity_id,
                    data={"fields": sorted(type(model).model_fields)},
                ),
            )

    async def reveal_secret(
        self, identity_id: UUID, field: str, *, actor: str, reason: str | None = None
    ) -> dict[str, Any]:
        """Раскрыть секрет для админки: `credentials`, `session[:slot]`, `profile`, `evidence:<factor>`.

        Событие аудита пишется в той же транзакции, что и чтение (S-7): без записи нет и раскрытия.
        """
        if not actor:
            raise ValidationFailed("раскрытие секрета требует автора")
        kind, _, key = field.partition(":")
        envelope = self.hub.envelope
        async with self._tx(own=True) as tx:
            row = await self._identity(tx, identity_id)
            service = self.hub.service(row.service)
            if kind == "credentials":
                payload = await tx.get_credentials(identity_id)
                value = (
                    envelope.open_model(
                        service.credentials, payload, associated_data("ih_credentials", identity_id)
                    )
                    if payload
                    else None
                )
            elif kind == "session":
                slot = key or "default"
                found = await tx.get_session(identity_id, slot)
                value = (
                    envelope.open_model(
                        service.session, found[0], associated_data("ih_sessions", identity_id, slot)
                    )
                    if found
                    else None
                )
            elif kind == "profile":
                secret = await tx.get_profile_secret(identity_id)
                value = (
                    envelope.open_json(secret, associated_data("ih_identities", identity_id, "profile"))
                    if secret
                    else None
                )
            elif kind == "evidence" and key:
                spec = service.verification(key)
                ver = await tx.load_verification(identity_id, key)
                value = (
                    envelope.open_model(
                        spec.evidence, ver.evidence, associated_data("ih_verifications", identity_id, key)
                    )
                    if spec.evidence and ver and ver.evidence
                    else None
                )
            else:
                raise ValidationFailed(f"неизвестное поле секрета {field!r}")
            if value is None:
                raise NotFound("secret", f"{identity_id}/{field}")
            await self._emit(
                tx,
                EventDraft(
                    "identity.secret.revealed",
                    actor=actor,
                    service=row.service,
                    identity_id=identity_id,
                    data={"field": field, "reason": reason},
                ),
            )
        revealed: dict[str, Any] = reveal(value)
        return revealed
