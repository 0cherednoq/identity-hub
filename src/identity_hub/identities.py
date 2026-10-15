"""Регистрация и данные аккаунтов (ТЗ FR-2.1–2.3, FR-3, G-4.3)."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from identity_hub._ids import new_id
from identity_hub._space import SpaceBase
from identity_hub.crypto import StoredPayload, associated_data, credential_associated_data
from identity_hub.domain.credentials import CredentialInput
from identity_hub.domain.data import (
    build_search_text,
    normalize_tag,
    split_profile,
    validate_json_payload,
    validate_labels,
    validate_model,
    validate_tags,
)
from identity_hub.domain.regions import Command, RegionState
from identity_hub.enums import AuthState, ChangeSource, Lifecycle, Standing
from identity_hub.errors import AlreadyExists, NotFound, ValidationFailed
from identity_hub.events import EventDraft
from identity_hub.service import Service
from identity_hub.state import DEFAULT_SLOT, apply_region_change
from identity_hub.store.base import IdentityRow, NewIdentity, SearchFilter

__all__ = ["IdentitiesMixin", "PreparedIdentity", "RegisterResult"]


class _Unset:
    """Маркер «аргумент не передан» там, где `None` — осмысленное значение."""

    def __repr__(self) -> str:
        return "UNSET"


UNSET = _Unset()


@dataclass(frozen=True, slots=True)
class RegisterResult:
    identity_id: UUID
    created: bool  # False — аккаунт уже был, ничего не изменено (FR-2.3)


@dataclass(frozen=True, slots=True)
class PreparedCredential:
    """Запись credentials, проверенная схемой и зашифрованная до транзакции (D-217)."""

    kind: str
    name: str
    payload: StoredPayload
    public_value: dict[str, Any] | None
    expires_at: datetime | None
    value_fingerprint: str


@dataclass(frozen=True, slots=True)
class PreparedIdentity:
    """Аккаунт, провалидированный и зашифрованный до открытия транзакции (T-5). Используется и импортом."""

    row: NewIdentity
    credentials: StoredPayload | None
    sessions: dict[str, StoredPayload]
    session_expirations: dict[str, datetime | None]
    credential_fingerprint: str | None
    session_fingerprints: dict[str, str]
    links: dict[str, UUID]
    credential_records: tuple[PreparedCredential, ...] = ()


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
    credential_records: Iterable[CredentialInput] = (),
) -> PreparedIdentity:
    """Валидация схемами сервиса и шифрование секретов (FR-2.4: до обращения к БД)."""
    normalized_ref = service.normalize_external_ref(external_ref)  # IMP-08: до поиска и вставки
    if str(lifecycle) not in set(Lifecycle):
        raise ValidationFailed(f"lifecycle: неизвестное значение {lifecycle!r}")
    identity_id = identity_id or new_id()
    envelope = space.hub._envelope

    attrs = validate_model(service.attributes, attributes or {}, "attributes").model_dump(mode="json")
    profile_model = validate_model(service.profile, profile or {}, "profile")
    open_profile, secret_profile = split_profile(service, profile_model)
    clean_labels = validate_labels(labels or {})
    clean_tags = validate_tags(tags)
    validate_json_payload(attrs, "attributes")
    validate_json_payload(profile_model.model_dump(mode="json"), "profile")
    validate_json_payload(clean_labels, "labels")

    creds_payload = None
    credential_fingerprint = None
    if credentials is not None:
        creds = validate_model(service.credentials, credentials, "credentials")
        creds_payload = space.hub._seal_stored_model(creds, associated_data("ih_credentials", identity_id))
        credential_fingerprint = space.hub._value_fingerprint(creds)
    records: list[PreparedCredential] = []
    seen: set[tuple[str, str]] = {("default", "default")} if credentials is not None else set()
    for record in credential_records:
        kind, name = service.credential_key(record.kind, record.name)
        if (kind, name) in seen:
            raise ValidationFailed(f"credentials {kind}/{name}: запись передана дважды")
        seen.add((kind, name))
        model = validate_model(service.credential_schema(kind), record.value, f"credentials {kind}/{name}")
        public = dict(record.public_value) if record.public_value is not None else None
        if public is not None:
            validate_json_payload(public, f"public_value {kind}/{name}")
        sealed = space.hub._seal_stored_model(model, credential_associated_data(identity_id, kind, name))
        records.append(
            PreparedCredential(
                kind,
                name,
                sealed,
                public,
                record.expires_at,
                space.hub._value_fingerprint(model),
            )
        )
    session_payloads: dict[str, StoredPayload] = {}
    session_expirations: dict[str, datetime | None] = {}
    session_fingerprints: dict[str, str] = {}
    for slot, data in (sessions or {}).items():
        model = validate_model(
            service.session_schema(slot), service.session_input(slot, data), f"sessions.{slot}"
        )
        session_payloads[slot] = space.hub._seal_stored_model(
            model, associated_data("ih_sessions", identity_id, slot)
        )
        session_expirations[slot] = service.session_expiry(model)
        session_fingerprints[slot] = space.hub._value_fingerprint(model)
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
        normalized_ref=normalized_ref,
        normalize_version=service.normalize_ref_version,
    )
    return PreparedIdentity(
        row=row,
        credentials=creds_payload,
        sessions=session_payloads,
        session_expirations=session_expirations,
        credential_fingerprint=credential_fingerprint,
        session_fingerprints=session_fingerprints,
        links=link_map,
        credential_records=tuple(records),
    )


class IdentitiesMixin(SpaceBase):
    """Регистрация и данные аккаунта."""

    async def _register(
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
            return await self._create_one(tx, svc, prepared, actor=actor)

    async def _create_one(
        self, tx: Any, svc: Service, prepared: PreparedIdentity, *, actor: str, fail_existing: bool = False
    ) -> RegisterResult:
        """Вставить подготовленный аккаунт; существующий вернуть (`created=False`) или бросить."""
        existing = await self._existing(tx, prepared.row)
        if existing is None and not await tx.insert_identity(prepared.row):
            existing = await self._existing(tx, prepared.row)
            if existing is None:  # pragma: no cover - конфликт без найденной строки
                raise AlreadyExists("identity", prepared.row.external_ref)
        if existing is not None:
            if fail_existing:
                raise AlreadyExists(
                    "identity", f"{prepared.row.service}:{prepared.row.external_ref}", existing.identity_id
                )
            return existing
        await self._write_children(tx, prepared, source=ChangeSource.COMMAND)
        await self._emit_registered(tx, svc, prepared, actor=actor)
        return RegisterResult(prepared.row.id, created=True)

    async def _emit_registered(
        self, tx: Any, svc: Service, prepared: PreparedIdentity, *, actor: str
    ) -> None:
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

    async def _existing(self, tx: Any, row: NewIdentity) -> RegisterResult | None:
        found = await tx.find_identity(row.id, row.service, row.unique_ref)
        if found is None:
            return None
        found_id, found_space = found
        if found_space != tx.space.id:
            # id чужого пространства наружу не отдаётся: это утечка между пространствами (R2-D19)
            raise AlreadyExists("identity", f"{row.service}:{row.external_ref}")
        return RegisterResult(found_id, created=False)

    async def _write_children_command(self, tx: Any, prepared: PreparedIdentity) -> None:
        await self._write_children(tx, prepared, source=ChangeSource.COMMAND)

    async def _write_children(self, tx: Any, prepared: PreparedIdentity, *, source: ChangeSource) -> None:
        """Секреты, сессии, начальные регионы и связи только что вставленного аккаунта."""
        identity_id = prepared.row.id
        if prepared.credentials is not None:
            await tx.put_credentials(
                identity_id,
                prepared.credentials,
                value_fingerprint=prepared.credential_fingerprint,
            )
        for record in prepared.credential_records:  # в той же транзакции, что и аккаунт (D-217)
            await tx.put_credential(
                identity_id,
                record.kind,
                record.name,
                record.payload,
                public_value=record.public_value,
                expires_at=record.expires_at,
                value_fingerprint=record.value_fingerprint,
            )
        for slot, payload in prepared.sessions.items():
            await tx.put_session(
                identity_id,
                slot,
                payload,
                fencing=0,
                expires_at=prepared.session_expirations.get(slot),
                value_fingerprint=prepared.session_fingerprints.get(slot),
            )
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

    # --------------------------------------------------------- изменение данных (FR-3, общая транзакция)

    async def _set_labels(
        self, identity_id: UUID, labels: Mapping[str, Any], *, merge: bool = False, actor: str = "system"
    ) -> IdentityRow:
        """Метки для фильтров (FR-3.3). `merge=True` — дополнить; значение None удаляет ключ."""
        validate_json_payload(dict(labels), "labels")

        def mutate(row: IdentityRow, _: dict[str, Any]) -> dict[str, Any]:
            base = dict(row.labels) if merge else {}
            for key, value in labels.items():
                if value is None:
                    base.pop(key, None)
                else:
                    base[key] = value
            return {"labels": dict(validate_labels(base))}

        return await self._mutate(identity_id, actor, mutate)

    async def _set_tags(
        self, identity_id: UUID, tags: Iterable[str], *, actor: str = "system"
    ) -> IdentityRow:
        return await self._mutate(identity_id, actor, lambda row, _: {"tags": validate_tags(tags)})

    async def _add_tags(
        self, identity_id: UUID, tags: Iterable[str], *, actor: str = "system"
    ) -> IdentityRow:
        return await self._mutate(
            identity_id, actor, lambda row, _: {"tags": validate_tags([*row.tags, *tags])}
        )

    async def _remove_tags(
        self, identity_id: UUID, tags: Iterable[str], *, actor: str = "system"
    ) -> IdentityRow:
        drop = {normalize_tag(tag) for tag in tags}
        return await self._mutate(
            identity_id, actor, lambda row, _: {"tags": validate_tags(t for t in row.tags if t not in drop)}
        )

    async def _update_many(
        self,
        *,
        search_filter: SearchFilter | None,
        identity_ids: tuple[UUID, ...] | None,
        add_tags: tuple[str, ...],
        remove_tags: tuple[str, ...],
        set_labels: dict[str, Any] | None,
        lifecycle: Lifecycle | None,
        actor: str,
        limit: int,
    ) -> tuple[int, int]:
        """Одна транзакция массовой команды; публичный DTO живёт в api.identities."""
        validate_tags(add_tags)
        if set_labels is not None:
            validate_json_payload(set_labels, "labels")
        if identity_ids is not None and len(set(identity_ids)) > limit:
            raise ValidationFailed(f"update_many выбрал больше {limit} identity")

        async with self._tx() as tx:
            if search_filter is not None:
                rows, _ = await tx.search_identities(
                    search_filter, limit=limit + 1, after=None, count_limit=None
                )
                if len(rows) > limit:
                    raise ValidationFailed(f"update_many выбрал больше {limit} identity")
                ids = sorted(row.id for row in rows)
            else:
                ids = sorted(set(identity_ids or ()))
            await tx.lock_identities(ids)

            changed = 0
            skipped = 0
            removed = set(remove_tags)
            for identity_id in ids:
                row = await self._identity(tx, identity_id)
                values: dict[str, Any] = {}
                tags = validate_tags(tag for tag in [*row.tags, *add_tags] if tag not in removed)
                if tags != row.tags:
                    values["tags"] = tags
                if set_labels is not None:
                    labels = dict(row.labels)
                    for key, value in set_labels.items():
                        if value is None:
                            labels.pop(key, None)
                        else:
                            labels[key] = value
                    normalized_labels = dict(validate_labels(labels))
                    if normalized_labels != row.labels:
                        values["labels"] = normalized_labels
                        values["search_text"] = build_search_text(
                            self.hub.service(row.service),
                            row.id,
                            row.external_ref,
                            row.profile,
                            normalized_labels,
                            row.note,
                        )
                data_changed = bool(values)
                if data_changed:
                    await tx.update_identity(identity_id, values)
                    event_data: dict[str, Any] = {"fields": sorted(k for k in values if k != "search_text")}
                    for public in ("labels", "tags"):
                        if public in values:
                            event_data[public] = values[public]
                    await self._emit(
                        tx,
                        EventDraft(
                            "identity.updated",
                            actor=actor,
                            service=row.service,
                            identity_id=identity_id,
                            data=event_data,
                        ),
                    )
                lifecycle_changed = False
                if lifecycle is not None:
                    decision = await apply_region_change(
                        self,
                        tx,
                        identity_id,
                        "lifecycle",
                        "",
                        Command(lifecycle, actor=actor),
                    )
                    lifecycle_changed = decision.changed
                if data_changed or lifecycle_changed:
                    changed += 1
                else:
                    skipped += 1
            return changed, skipped

    async def _set_attributes(
        self, identity_id: UUID, attributes: BaseModel | Mapping[str, Any], *, actor: str = "system"
    ) -> IdentityRow:
        """Технические параметры, валидируются схемой сервиса (FR-3.1)."""
        validate_json_payload(
            attributes.model_dump(mode="json") if isinstance(attributes, BaseModel) else dict(attributes),
            "attributes",
        )

        def mutate(row: IdentityRow, _: dict[str, Any]) -> dict[str, Any]:
            schema = self.hub.service(row.service).attributes
            return {"attributes": validate_model(schema, attributes, "attributes").model_dump(mode="json")}

        return await self._mutate(identity_id, actor, mutate)

    async def _update_profile(
        self,
        identity_id: UUID,
        changes: BaseModel | Mapping[str, Any],
        *,
        replace: bool = False,
        actor: str = "system",
    ) -> IdentityRow:
        """Профиль персоны (FR-3.2): слияние с текущим (или замена), валидация схемой сервиса."""
        validate_json_payload(
            changes.model_dump(mode="json") if isinstance(changes, BaseModel) else dict(changes), "profile"
        )

        def mutate(row: IdentityRow, current: dict[str, Any]) -> dict[str, Any]:
            service = self.hub.service(row.service)
            incoming = (
                changes.model_dump(exclude_unset=True) if isinstance(changes, BaseModel) else dict(changes)
            )
            merged = incoming if replace else {**current, **incoming}
            merged = {k: v for k, v in merged.items() if v is not None}
            model = validate_model(service.profile, merged, "profile")
            open_part, secret = split_profile(service, model)
            payload = (
                self.hub._envelope.seal_model(secret, associated_data("ih_identities", row.id, "profile"))
                if secret
                else None
            )
            return {"profile": open_part, _PROFILE_SECRET: payload}

        return await self._mutate(identity_id, actor, mutate, need_profile=True)

    async def _set_note(self, identity_id: UUID, note: str | None, *, actor: str = "system") -> IdentityRow:
        """Свободная заметка из админки (FR-3.5)."""
        return await self._mutate(identity_id, actor, lambda row, _: {"note": note or None})

    async def _update(
        self,
        identity_id: UUID,
        *,
        profile: BaseModel | Mapping[str, Any] | None = None,
        labels: Mapping[str, Any] | None = None,
        note: str | _Unset | None = UNSET,
        actor: str = "system",
    ) -> IdentityRow:
        """Слияние профиля и меток и замена заметки одной транзакцией и одним событием.

        `None` в `labels` или в полях профиля удаляет ключ; `note=None` очищает заметку.
        """
        profile_changes = (
            profile.model_dump(exclude_unset=True) if isinstance(profile, BaseModel) else profile
        )
        if profile_changes is not None:
            validate_json_payload(profile_changes, "profile")
        if labels is not None:
            validate_json_payload(dict(labels), "labels")

        def mutate(row: IdentityRow, current: dict[str, Any]) -> dict[str, Any]:
            values: dict[str, Any] = {}
            if labels is not None:
                merged_labels = dict(row.labels)
                for key, value in labels.items():
                    if value is None:
                        merged_labels.pop(key, None)
                    else:
                        merged_labels[key] = value
                values["labels"] = dict(validate_labels(merged_labels))
            if not isinstance(note, _Unset):
                values["note"] = note or None
            if profile_changes is not None:
                service = self.hub.service(row.service)
                merged = {k: v for k, v in {**current, **profile_changes}.items() if v is not None}
                model = validate_model(service.profile, merged, "profile")
                open_part, secret = split_profile(service, model)
                values["profile"] = open_part
                values[_PROFILE_SECRET] = (
                    self.hub._envelope.seal_model(secret, associated_data("ih_identities", row.id, "profile"))
                    if secret
                    else None
                )
            return values

        return await self._mutate(identity_id, actor, mutate, need_profile=profile_changes is not None)

    async def _delete(self, identity_id: UUID, *, actor: str = "system", revoke: bool = False) -> None:
        """Удалить аккаунт. Нельзя, пока на него ссылаются связи (FR-4.2); журналы переживают удаление.

        Аккаунт с живой арендой не удаляется: воркер продолжал бы работать с ним, а история аренды
        осталась бы открытой. `revoke=True` отзывает аренду в той же транзакции.
        """
        async with self._tx() as tx:
            await tx.lock_identities([identity_id])  # новая аренда не появится до конца транзакции
            row = await self._identity(tx, identity_id)
            incoming = await tx.incoming_links(identity_id)
            if incoming:
                refs = [f"{dependent} ({role})" for dependent, role in incoming]
                raise ValidationFailed("на аккаунт ссылаются связи — сначала удалите их", refs)
            if await tx.current_lease(identity_id) is not None:
                if not revoke:
                    raise ValidationFailed(
                        "у аккаунта живая аренда — дождитесь её освобождения или удалите с revoke=True"
                    )
                revoked = await tx.revoke_lease(identity_id)
                if revoked is not None:
                    await self._emit(
                        tx,
                        EventDraft(
                            "lease.revoked",
                            actor=actor,
                            service=row.service,
                            identity_id=identity_id,
                            lease_id=revoked.lease_id,
                            data={
                                "pool": revoked.pool,
                                "holder": revoked.holder,
                                "reason": "identity deleted",
                            },
                        ),
                    )
            await self._emit(
                tx,
                EventDraft(
                    "identity.deleted",
                    actor=actor,
                    service=row.service,
                    identity_id=identity_id,
                    data={"external_ref": row.external_ref},
                ),
            )
            await tx.delete_identity(identity_id)

    async def _mutate(
        self,
        identity_id: UUID,
        actor: str,
        mutate: Callable[[IdentityRow, dict[str, Any]], dict[str, Any]],
        *,
        need_profile: bool = False,
    ) -> IdentityRow:
        async with self._tx() as tx:
            # Чтение, слияние в Python и запись — под блокировкой строки: иначе параллельные
            # add_tags и слияния меток затирают друг друга (R2-D02).
            await tx.lock_identities([identity_id])
            row = await self._identity(tx, identity_id)
            profile = dict(row.profile)
            if need_profile:
                secret = await tx.get_profile_secret(identity_id)
                if secret is not None:
                    aad = associated_data("ih_identities", identity_id, "profile")
                    profile |= self.hub._envelope.open_json(secret, aad)
            values = mutate(row, profile)
            for field in ("profile", "labels", "attributes"):
                if field in values:
                    validate_json_payload(values[field], field)
            has_secret = _PROFILE_SECRET in values
            secret_payload = values.pop(_PROFILE_SECRET, None)
            changed = sorted(k for k, v in values.items() if getattr(row, k) != v)
            if has_secret:
                await tx.set_profile_secret(identity_id, secret_payload)
                if "profile" not in changed:
                    changed.append("profile")
            if not changed:
                return row
            service = self.hub.service(row.service)
            merged = {**_row_values(row), **values}
            values["search_text"] = build_search_text(
                service, identity_id, row.external_ref, merged["profile"], merged["labels"], merged["note"]
            )
            await tx.update_identity(identity_id, values)
            data: dict[str, Any] = {"fields": changed}
            for public in ("labels", "tags"):
                if public in changed:
                    data[public] = values[public]
            await self._emit(
                tx,
                EventDraft(
                    "identity.updated", actor=actor, service=row.service, identity_id=identity_id, data=data
                ),
            )
            updated = await tx.get_identity(identity_id)
            assert updated is not None
            return updated


_PROFILE_SECRET = "__profile_secret__"


def _row_values(row: IdentityRow) -> dict[str, Any]:
    return {"profile": row.profile, "labels": row.labels, "note": row.note}
