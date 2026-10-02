"""Секреты из админки (ТЗ FR-5.2, S-7): обновление credentials и раскрытие с аудитом.

Листинги, поиск и read-модели секретов не содержат; единственный путь увидеть секрет вне аренды —
`reveal_secret`, который пишет событие `identity.secret.revealed` (кто, когда, какой аккаунт, какое поле).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from identity_hub._space import SpaceBase, require_actor_and_reason
from identity_hub.api._base import legacy
from identity_hub.crypto import associated_data, reveal
from identity_hub.domain.data import validate_model
from identity_hub.errors import NotFound, ValidationFailed
from identity_hub.events import EventDraft

__all__ = ["SecretsAdminMixin"]


class SecretsAdminMixin(SpaceBase):
    async def _update_credentials(
        self, identity_id: UUID, credentials: BaseModel | Mapping[str, Any], *, actor: str
    ) -> None:
        """Заменить credentials (валидация схемой сервиса, шифрование до транзакции)."""
        async with self._tx(own=True) as tx:
            service = self.hub.service((await self._identity(tx, identity_id)).service)
        model = validate_model(service.credentials, credentials, "credentials")
        payload = self.hub._envelope.seal_model(model, associated_data("ih_credentials", identity_id))
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

    async def _update_session(
        self,
        identity_id: UUID,
        session: BaseModel | Mapping[str, Any],
        *,
        actor: str,
        slot: str = "default",
        expires_at: datetime | None = None,
    ) -> None:
        """Replace one session slot at the current fencing boundary."""
        if not slot or len(slot) > 64:
            raise ValidationFailed("session slot must contain from 1 to 64 characters")
        async with self._tx(own=True) as tx:
            service = self.hub.service((await self._identity(tx, identity_id)).service)
        model = validate_model(service.session, session, "session")
        payload = self.hub._envelope.seal_model(model, associated_data("ih_sessions", identity_id, slot))
        async with self._tx() as tx:
            row = await self._identity(tx, identity_id)
            fencing = await tx.current_fencing(identity_id, lock=True)
            if fencing is None or not await tx.put_session(
                identity_id, slot, payload, fencing=fencing, expires_at=expires_at
            ):
                raise ValidationFailed("session update lost its fencing boundary")
            await self._emit(
                tx,
                EventDraft(
                    "identity.session.updated",
                    actor=actor,
                    service=row.service,
                    identity_id=identity_id,
                    data={
                        "slot": slot,
                        "fields": sorted(type(model).model_fields),
                        "expires_at": expires_at,
                    },
                ),
            )

    async def _reveal_secret(
        self, identity_id: UUID, field: str, *, actor: str, reason: str
    ) -> dict[str, Any]:
        """Раскрыть секрет для админки: `credentials`, `session[:slot]`, `profile`, `evidence:<factor>`.

        Событие аудита пишется в той же транзакции, что и чтение (S-7): без записи нет и раскрытия.
        """
        actor, reason = require_actor_and_reason(actor, reason, "раскрытие секрета")
        kind, _, key = field.partition(":")
        envelope = self.hub._envelope
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

    update_credentials = legacy(_update_credentials, "update_credentials")
    update_session = legacy(_update_session, "update_session")
    reveal_secret = legacy(_reveal_secret, "reveal_secret")
