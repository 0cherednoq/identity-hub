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
from pydantic_core import to_jsonable_python

from identity_hub._space import SpaceBase, require_actor_and_reason
from identity_hub.crypto import associated_data, reveal
from identity_hub.domain.data import validate_model
from identity_hub.domain.idempotency import SECRETS_FAMILY, keyed_fingerprint, namespace
from identity_hub.errors import NotFound, ValidationFailed
from identity_hub.events import EventDraft
from identity_hub.store.base import IdempotencyRequest, StoreTx

__all__ = ["SecretsAdminMixin"]


class SecretsAdminMixin(SpaceBase):
    async def _update_credentials(
        self,
        identity_id: UUID,
        credentials: BaseModel | Mapping[str, Any],
        *,
        actor: str,
        idempotency_key: str | None = None,
    ) -> None:
        """Заменить credentials (валидация схемой сервиса, шифрование до транзакции)."""
        async with self._tx(own=True) as tx:
            service = self.hub.service((await self._identity(tx, identity_id)).service)
        model = validate_model(service.credentials, credentials, "credentials")
        payload = self.hub._envelope.seal_model(model, associated_data("ih_credentials", identity_id))
        command = {"identity_id": identity_id}
        async with self._tx(checked=idempotency_key is None) as tx:
            if not await self._claim_secret_command(
                tx, idempotency_key, "replace_credentials", command, model
            ):
                return
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
            await self._complete_secret_command(tx, idempotency_key)

    async def _update_session(
        self,
        identity_id: UUID,
        session: BaseModel | Mapping[str, Any],
        *,
        actor: str,
        slot: str = "default",
        expires_at: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        """Replace one session slot at the current fencing boundary."""
        if not slot or len(slot) > 64:
            raise ValidationFailed("session slot must contain from 1 to 64 characters")
        async with self._tx(own=True) as tx:
            service = self.hub.service((await self._identity(tx, identity_id)).service)
        model = validate_model(service.session, session, "session")
        payload = self.hub._envelope.seal_model(model, associated_data("ih_sessions", identity_id, slot))
        command = {"identity_id": identity_id, "slot": slot, "expires_at": expires_at}
        async with self._tx(checked=idempotency_key is None) as tx:
            if not await self._claim_secret_command(tx, idempotency_key, "replace_session", command, model):
                return
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
            await self._complete_secret_command(tx, idempotency_key)

    async def _claim_secret_command(
        self, tx: StoreTx, key: str | None, operation: str, command: Mapping[str, Any], model: BaseModel
    ) -> bool:
        """Занять ключ команды с секретами (D-208); `False` — повтор уже выполненной команды.

        Отпечаток — по HMAC-тегам секрета ключом отпечатков: ни секрет, ни тег не хранятся.
        Ключ берётся первым (порядок блокировок, п. 0), затем сверяется `expected_revision`.
        """
        if key is None:
            return True
        if not key or len(key) > 256:
            raise ValidationFailed("idempotency_key: от 1 до 256 символов")
        keyring = self.hub._fingerprints
        keyed = keyed_fingerprint(
            operation,
            command,
            {"value": to_jsonable_python(reveal(model.model_dump()))},
            tags=keyring.tags,
            active_key_id=keyring.active_key_id,
        )
        claim = await tx.claim_idempotency_key(
            IdempotencyRequest(
                namespace=namespace(tx.space.id, SECRETS_FAMILY),
                key=key,
                operation=operation,
                fingerprint=keyed.fingerprint,
                ttl=self.hub.retention.operation_keys,
                owner_identity_id=command["identity_id"],
                fingerprint_key_id=keyed.key_id,
                alternates=keyed.alternates,
            )
        )
        await self._check_expected(tx)
        return claim.claimed

    @staticmethod
    async def _complete_secret_command(tx: StoreTx, key: str | None) -> None:
        if key is not None:
            await tx.complete_idempotency_key(namespace(tx.space.id, SECRETS_FAMILY), key, "succeeded")

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
