"""Регистрация и данные аккаунтов (ТЗ FR-2.1–2.3, FR-3, G-4.3)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from identity_hub._ids import new_id
from identity_hub._space import SpaceBase
from identity_hub.crypto import EncryptedPayload, associated_data
from identity_hub.domain.data import (
    build_search_text,
    split_profile,
    validate_labels,
    validate_model,
    validate_tags,
)
from identity_hub.domain.regions import RegionState
from identity_hub.enums import AuthState, ChangeSource, Lifecycle, Standing
from identity_hub.errors import AlreadyExists, NotFound, ValidationFailed
from identity_hub.events import EventDraft
from identity_hub.service import Service
from identity_hub.state import DEFAULT_SLOT
from identity_hub.store.base import NewIdentity

__all__ = ["IdentitiesMixin", "PreparedIdentity", "RegisterResult"]


@dataclass(frozen=True, slots=True)
class RegisterResult:
    identity_id: UUID
    created: bool  # False — аккаунт уже был, ничего не изменено (FR-2.3)


@dataclass(frozen=True, slots=True)
class PreparedIdentity:
    """Аккаунт, провалидированный и зашифрованный до открытия транзакции (T-5). Используется и импортом."""

    row: NewIdentity
    credentials: EncryptedPayload | None
    sessions: dict[str, EncryptedPayload]
    links: dict[str, UUID]


def prepare_identity(
    space: SpaceBase,
    service: Service,
    *,
    external_ref: str,
    identity_id: UUID | None = None,
    credentials: BaseModel | Mapping[str, Any] | None = None,
    sessions: Mapping[str, BaseModel | Mapping[str, Any]] | None = None,
    attributes: BaseModel | Mapping[str, Any] | None = None,
    profile: BaseModel | Mapping[str, Any] | None = None,
    labels: Mapping[str, Any] | None = None,
    tags: Iterable[str] = (),
    note: str | None = None,
    links: Mapping[str, UUID] | None = None,
    lifecycle: Lifecycle | str = Lifecycle.PROVISIONING,
    auth_state: AuthState | str | None = None,
) -> PreparedIdentity:
    """Валидация схемами сервиса и шифрование секретов (FR-2.4: до обращения к БД)."""
    if not external_ref or len(external_ref) > 256:
        raise ValidationFailed("external_ref: от 1 до 256 символов")
    if str(lifecycle) not in set(Lifecycle):
        raise ValidationFailed(f"lifecycle: неизвестное значение {lifecycle!r}")
    identity_id = identity_id or new_id()
    envelope = space.hub.envelope

    attrs = validate_model(service.attributes, attributes or {}, "attributes").model_dump(mode="json")
    profile_model = validate_model(service.profile, profile or {}, "profile")
    open_profile, secret_profile = split_profile(service, profile_model)
    clean_labels = validate_labels(labels or {})
    clean_tags = validate_tags(tags)

    creds_payload = None
    if credentials is not None:
        creds = validate_model(service.credentials, credentials, "credentials")
        creds_payload = envelope.seal_model(creds, associated_data("ih_credentials", identity_id))
    session_payloads: dict[str, EncryptedPayload] = {}
    for slot, data in (sessions or {}).items():
        model = validate_model(service.session, data, f"sessions.{slot}")
        session_payloads[slot] = envelope.seal_model(model, associated_data("ih_sessions", identity_id, slot))
    profile_secret = (
        envelope.seal_model(secret_profile, associated_data("ih_identities", identity_id, "profile"))
        if secret_profile
        else None
    )

    link_map = dict(links or {})
    if identity_id in link_map.values():
        raise ValidationFailed("связь аккаунта с самим собой запрещена (FR-4.2)")

    if auth_state is None:
        auth_state = AuthState.AUTHENTICATED if DEFAULT_SLOT in session_payloads else AuthState.NO_SESSION
    row = NewIdentity(
        id=identity_id,
        service=service.name,
        external_ref=external_ref,
        lifecycle=str(lifecycle),
        standing=Standing.GOOD,
        auth_state=str(auth_state),
        tags=clean_tags,
        attributes=attrs,
        profile=open_profile,
        labels=dict(clean_labels),
        note=note,
        search_text=build_search_text(service, identity_id, external_ref, open_profile, clean_labels, note),
        profile_secret=profile_secret,
    )
    return PreparedIdentity(row=row, credentials=creds_payload, sessions=session_payloads, links=link_map)


class IdentitiesMixin(SpaceBase):
    """Регистрация и данные аккаунта."""

    async def register(
        self,
        *,
        service: Service | str,
        external_ref: str,
        id: UUID | None = None,
        credentials: BaseModel | Mapping[str, Any] | None = None,
        session: BaseModel | Mapping[str, Any] | None = None,
        sessions: Mapping[str, BaseModel | Mapping[str, Any]] | None = None,
        attributes: BaseModel | Mapping[str, Any] | None = None,
        profile: BaseModel | Mapping[str, Any] | None = None,
        labels: Mapping[str, Any] | None = None,
        tags: Iterable[str] = (),
        note: str | None = None,
        links: Mapping[str, UUID] | None = None,
        lifecycle: Lifecycle | str = Lifecycle.PROVISIONING,
        auth_state: AuthState | str | None = None,
        actor: str = "system",
    ) -> RegisterResult:
        """Создать аккаунт (FR-2.1). Идемпотентно по `id` и `(service, external_ref)` (FR-2.3, D-110).

        Аккаунт, секреты, сессии, начальные регионы, связи и событие — одна транзакция
        (в сессии пользователя — savepoint, G-4.3).
        """
        svc = self.hub.service(service)
        all_sessions = dict(sessions or {})
        if session is not None:
            all_sessions[DEFAULT_SLOT] = session
        prepared = prepare_identity(
            self,
            svc,
            external_ref=external_ref,
            identity_id=id,
            credentials=credentials,
            sessions=all_sessions,
            attributes=attributes,
            profile=profile,
            labels=labels,
            tags=tags,
            note=note,
            links=links,
            lifecycle=lifecycle,
            auth_state=auth_state,
        )
        async with self._tx() as tx:
            existing = await self._existing(tx, prepared.row)
            if existing is not None:
                return existing
            if not await tx.insert_identity(prepared.row):
                existing = await self._existing(tx, prepared.row)
                if existing is not None:
                    return existing
                raise AlreadyExists("identity", prepared.row.external_ref)  # pragma: no cover
            await self._write_children(tx, prepared, source=ChangeSource.COMMAND)
            await self._emit(
                tx,
                EventDraft(
                    "identity.registered",
                    actor=actor,
                    service=svc.name,
                    identity_id=prepared.row.id,
                    data={
                        "external_ref": prepared.row.external_ref,
                        "lifecycle": prepared.row.lifecycle,
                        "auth": prepared.row.auth_state,
                        "tags": prepared.row.tags,
                        "labels": prepared.row.labels,
                        "links": {role: str(target) for role, target in prepared.links.items()},
                    },
                ),
                *(
                    EventDraft(
                        "identity.linked",
                        actor=actor,
                        service=svc.name,
                        identity_id=prepared.row.id,
                        data={"role": role, "linked_identity_id": str(target)},
                    )
                    for role, target in prepared.links.items()
                ),
            )
        return RegisterResult(prepared.row.id, created=True)

    async def _existing(self, tx: Any, row: NewIdentity) -> RegisterResult | None:
        found = await tx.find_identity(row.id, row.service, row.external_ref)
        if found is None:
            return None
        found_id, found_space = found
        if found_space != tx.space.id:
            raise AlreadyExists("identity", f"{row.service}:{row.external_ref}", found_id)
        return RegisterResult(found_id, created=False)

    async def _write_children(self, tx: Any, prepared: PreparedIdentity, *, source: ChangeSource) -> None:
        """Секреты, сессии, начальные регионы и связи только что вставленного аккаунта."""
        identity_id = prepared.row.id
        if prepared.credentials is not None:
            await tx.put_credentials(identity_id, prepared.credentials)
        for slot, payload in prepared.sessions.items():
            await tx.put_session(identity_id, slot, payload, fencing=0)
        now = await tx.now()
        await tx.save_region(
            identity_id,
            "standing",
            "",
            None,
            RegionState(state=prepared.row.standing, changed_at=now, change_source=source),
        )
        await tx.save_region(
            identity_id,
            "auth",
            DEFAULT_SLOT,
            None,
            RegionState(state=prepared.row.auth_state, changed_at=now, change_source=source),
        )
        if prepared.links:
            targets = set(prepared.links.values())
            missing = targets - await tx.existing_identities(list(targets))
            if missing:
                raise NotFound("identity", ", ".join(sorted(map(str, missing))))
            for role, target in prepared.links.items():
                await tx.insert_link(identity_id, role, target)
