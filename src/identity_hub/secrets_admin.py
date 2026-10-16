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
from identity_hub.crypto import associated_data, credential_associated_data, reveal
from identity_hub.domain.credentials import (
    ACTIVE,
    DISABLED,
    REVOKED,
    check_transition,
    session_event_data,
    session_mode,
)
from identity_hub.domain.data import validate_json_payload, validate_model
from identity_hub.domain.idempotency import SECRETS_FAMILY, keyed_fingerprint, namespace
from identity_hub.domain.regions import Command
from identity_hub.enums import AuthState
from identity_hub.errors import NotFound, TransitionNotAllowed, ValidationFailed
from identity_hub.events import EventDraft
from identity_hub.state import DEFAULT_SLOT, apply_region_change
from identity_hub.store.base import CredentialRow, IdempotencyRequest, IdentityRow, StoreTx

__all__ = ["CredentialsMixin", "SecretsAdminMixin", "SessionRevokeMixin"]


async def _reset_auth_after_credentials(
    space: SpaceBase,
    tx: StoreTx,
    row: IdentityRow,
    *,
    actor: str,
) -> None:
    """Явно снять прежний отказ входа в транзакции записи новых credentials."""
    if row.auth_state in {AuthState.NO_SESSION, AuthState.SESSION_INVALID}:
        await apply_region_change(
            space,
            tx,
            row.id,
            "auth",
            DEFAULT_SLOT,
            Command(AuthState.NO_SESSION, actor=actor, clear_code=True),
        )


class SecretsAdminMixin(SpaceBase):
    async def _update_credentials(
        self,
        identity_id: UUID,
        credentials: BaseModel | Mapping[str, Any],
        *,
        actor: str,
        idempotency_key: str | None = None,
        reset_auth: bool = False,
    ) -> None:
        """Заменить credentials (валидация схемой сервиса, шифрование до транзакции)."""
        async with self._tx(own=True) as tx:
            service = self.hub.service((await self._identity(tx, identity_id)).service)
        model = validate_model(service.credentials, credentials, "credentials")
        payload = self.hub._seal_stored_model(model, associated_data("ih_credentials", identity_id))
        value_fingerprint = self.hub._value_fingerprint(model)
        command = {"identity_id": identity_id, "reset_auth": reset_auth}
        async with self._tx(checked=idempotency_key is None) as tx:
            if not await self._claim_secret_command(
                tx, idempotency_key, "replace_credentials", command, model
            ):
                return
            row = await self._identity(tx, identity_id)
            await tx.put_credentials(identity_id, payload, value_fingerprint=value_fingerprint)
            if reset_auth:
                await _reset_auth_after_credentials(self, tx, row, actor=actor)
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
        mode: str = "in_place",
    ) -> None:
        """Replace one session slot at the current fencing boundary."""
        mode = session_mode(mode)
        if not slot or len(slot) > 64:
            raise ValidationFailed("session slot must contain from 1 to 64 characters")
        async with self._tx(own=True) as tx:
            service = self.hub.service((await self._identity(tx, identity_id)).service)
        model = validate_model(
            service.session_schema(slot), service.session_input(slot, session), f"session {slot}"
        )
        if expires_at is None:
            expires_at = service.session_expiry(model)
        payload = self.hub._seal_stored_model(model, associated_data("ih_sessions", identity_id, slot))
        value_fingerprint = self.hub._value_fingerprint(model)
        command = {"identity_id": identity_id, "slot": slot, "expires_at": expires_at, "mode": mode}
        async with self._tx(checked=idempotency_key is None) as tx:
            if not await self._claim_secret_command(tx, idempotency_key, "replace_session", command, model):
                return
            row = await self._identity(tx, identity_id)
            fencing = await tx.current_fencing(identity_id, lock=True)
            written = (
                await tx.put_session(
                    identity_id,
                    slot,
                    payload,
                    fencing=fencing,
                    expires_at=expires_at,
                    mode=mode,
                    value_fingerprint=value_fingerprint,
                )
                if fencing is not None
                else None
            )
            if written is None:
                raise ValidationFailed("session update lost its fencing boundary")
            await self._emit(
                tx,
                EventDraft(
                    "identity.session.updated",
                    actor=actor,
                    service=row.service,
                    identity_id=identity_id,
                    data={
                        **session_event_data(slot, mode, written),
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
        audit: dict[str, str] = {}  # вид и имя записи credentials — в событие, значение — нет
        async with self._tx(own=True) as tx:
            row = await self._identity(tx, identity_id)
            service = self.hub.service(row.service)
            if kind == "credentials":
                payload = await tx.get_credentials(identity_id)
                value = (
                    self.hub._open_stored_model(
                        service.credentials, payload, associated_data("ih_credentials", identity_id)
                    )
                    if payload
                    else None
                )
            elif kind == "credential" and "/" in key:
                cred_kind, _, cred_name = key.partition("/")
                schema = service.credential_schema(cred_kind)
                record = await tx.get_credential(identity_id, cred_kind, cred_name)
                value = (
                    self.hub._open_stored_model(
                        schema, record[0], credential_associated_data(identity_id, cred_kind, cred_name)
                    )
                    if record
                    else None
                )
                audit = {"kind": cred_kind, "name": cred_name}
            elif kind == "session":
                slot = key or "default"
                found = await tx.get_session(identity_id, slot)
                value = (
                    self.hub._open_stored_model(
                        service.session_schema(slot),
                        found[0],
                        associated_data("ih_sessions", identity_id, slot),
                    )
                    if found
                    else None
                )
            elif kind == "profile":
                secret = await tx.get_profile_secret(identity_id)
                value = (
                    self.hub._open_stored_json(
                        secret, associated_data("ih_identities", identity_id, "profile")
                    )
                    if secret
                    else None
                )
            elif kind == "evidence" and key:
                spec = service.verification(key)
                ver = await tx.load_verification(identity_id, key)
                value = (
                    self.hub._open_stored_model(
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
                    data={"field": field, "reason": reason, **audit},
                ),
            )
        revealed: dict[str, Any] = reveal(value)
        return revealed


class CredentialsMixin(SpaceBase):
    """Несколько credentials аккаунта (IMP-09, D-215): запись, список, жизненный цикл."""

    async def _put_credential(
        self,
        identity_id: UUID,
        kind: str,
        value: BaseModel | Mapping[str, Any],
        *,
        name: str,
        public_value: Mapping[str, Any] | None,
        expires_at: datetime | None,
        actor: str,
        reset_auth: bool = False,
    ) -> CredentialRow:
        async with self._tx(own=True) as tx:
            service = self.hub.service((await self._identity(tx, identity_id)).service)
        kind, name = service.credential_key(kind, name)
        model = validate_model(service.credential_schema(kind), value, f"credentials {kind}")
        public = dict(public_value) if public_value is not None else None
        if public is not None:
            validate_json_payload(public, "public_value")  # открытая часть: только JSON, с лимитом размера
        payload = self.hub._seal_stored_model(model, credential_associated_data(identity_id, kind, name))
        value_fingerprint = self.hub._value_fingerprint(model)
        async with self._tx() as tx:
            row = await self._identity(tx, identity_id)
            current = await tx.get_credential(identity_id, kind, name)
            if current is not None and current[1].lifecycle == REVOKED:
                raise TransitionNotAllowed("credentials", REVOKED, ACTIVE, "revoked — конечное состояние")
            stored = await tx.put_credential(
                identity_id,
                kind,
                name,
                payload,
                public_value=public,
                expires_at=expires_at,
                value_fingerprint=value_fingerprint,
            )
            await self._credential_event(
                tx, row, kind, name, "put", actor, fields=sorted(type(model).model_fields)
            )
            if reset_auth:
                await _reset_auth_after_credentials(self, tx, row, actor=actor)
            return stored

    async def _credential_list(self, identity_id: UUID) -> tuple[list[CredentialRow], datetime]:
        async with self._tx(own=True) as tx:
            await self._identity(tx, identity_id)
            return await tx.list_credentials(identity_id), await tx.now()

    async def _set_credential_lifecycle(
        self, identity_id: UUID, kind: str, name: str, target: str, *, actor: str, reason: str | None = None
    ) -> bool:
        action = {ACTIVE: "enable", DISABLED: "disable", REVOKED: "revoke"}[target]
        async with self._tx() as tx:
            row = await self._identity(tx, identity_id)
            self.hub.service(row.service).credential_key(kind, name)
            current = await tx.get_credential(identity_id, kind, name)
            if current is None:
                raise NotFound("credentials", f"{identity_id}/{kind}/{name}")
            if current[1].lifecycle == REVOKED and target == REVOKED:
                return False  # повторный отзыв детерминирован
            if not check_transition(current[1].lifecycle, target):
                return False
            await tx.set_credential_lifecycle(identity_id, kind, name, target)
            await self._credential_event(tx, row, kind, name, action, actor, reason=reason)
            return True

    async def _credential_event(
        self,
        tx: StoreTx,
        row: Any,
        kind: str,
        name: str,
        action: str,
        actor: str,
        *,
        fields: list[str] | None = None,
        reason: str | None = None,
    ) -> None:
        data: dict[str, Any] = {"kind": kind, "name": name, "action": action}
        if fields is not None:
            data["fields"] = fields
        if reason is not None:
            data["reason"] = reason
        await self._emit(
            tx,
            EventDraft(
                "identity.credentials.updated",
                actor=actor,
                service=row.service,
                identity_id=row.id,
                data=data,
            ),
        )


class SessionRevokeMixin(SpaceBase):
    """Конечный отзыв сессии слота без замены (IMP-10, D-219)."""

    async def _revoke_session(self, identity_id: UUID, slot: str, *, actor: str, reason: str | None) -> bool:
        async with self._tx() as tx:
            row = await self._identity(tx, identity_id)
            if not await tx.revoke_session(identity_id, slot):
                return False
            data: dict[str, Any] = {"slot": slot, "mode": "revoke"}
            if reason is not None:
                data["reason"] = reason
            await self._emit(
                tx,
                EventDraft(
                    "identity.session.updated",
                    actor=actor,
                    service=row.service,
                    identity_id=identity_id,
                    data=data,
                ),
            )
            return True
