"""Durable runtime service definitions and their public management API."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, ValidationError

from identity_hub.api.identities import Page
from identity_hub.domain.redaction import sanitize_text
from identity_hub.errors import (
    AlreadyExists,
    IdempotencyConflict,
    NotFound,
    RevisionConflict,
    ValidationFailed,
)
from identity_hub.service import Service
from identity_hub.store.base import RuntimeServiceRow

if TYPE_CHECKING:
    from identity_hub.hub import IdentityHub

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None

__all__ = [
    "JsonValue",
    "RuntimeServiceCreate",
    "RuntimeServiceDefinition",
    "ServiceDefinitions",
    "ServiceTemplate",
]

_TEMPLATE_NAME = re.compile(r"[a-z0-9][a-z0-9_./-]{0,127}\Z")
_MAX_CONFIG_BYTES = 64 * 1024


class ServiceTemplate(Protocol):
    name: str
    config_schema: type[BaseModel]

    def build(self, definition: RuntimeServiceDefinition) -> Service[Any, Any]: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeServiceCreate:
    name: str
    title: str
    url: str | None
    template: str
    config: Mapping[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeServiceDefinition:
    name: str
    title: str
    url: str | None
    template: str
    config: Mapping[str, JsonValue]
    enabled: bool
    revision: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: RuntimeServiceRow) -> RuntimeServiceDefinition:
        return cls(
            row.name,
            row.title,
            row.url,
            row.template,
            dict(row.config),
            row.enabled,
            row.revision,
            row.created_at,
            row.updated_at,
        )


class _Unset:
    pass


UNSET = _Unset()


class ServiceDefinitions:
    """CRUD namespace exposed as ``IdentityHub.service_definitions``."""

    __slots__ = ("_hub",)

    def __init__(self, hub: IdentityHub) -> None:
        self._hub = hub

    async def create(self, spec: RuntimeServiceCreate, *, actor: str) -> RuntimeServiceDefinition:
        self._actor(actor)
        definition, service = await self._prepare_create(spec)
        row, created = await self._hub._store.create_service_definition(
            self._row(definition), idempotency_key=None, fingerprint=None, actor=actor
        )
        if not created:
            raise AlreadyExists("service", spec.name)
        result = RuntimeServiceDefinition.from_row(row)
        self._hub._cache_runtime_service(result, service)
        return result

    async def ensure(
        self, spec: RuntimeServiceCreate, *, actor: str, idempotency_key: str
    ) -> tuple[RuntimeServiceDefinition, bool]:
        self._actor(actor)
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 256:
            raise ValidationFailed("idempotency_key: from 1 to 256 characters")
        definition, service = await self._prepare_create(spec)
        fingerprint = self._hub._fingerprints.tag(self._canonical(spec))
        row, created = await self._hub._store.create_service_definition(
            self._row(definition),
            idempotency_key=idempotency_key,
            fingerprint=fingerprint,
            actor=actor,
        )
        if not created and row.idempotency_key == idempotency_key:
            if row.idempotency_fingerprint != fingerprint:
                raise IdempotencyConflict("service_definitions.ensure")
        elif not created:
            raise AlreadyExists("service", spec.name)
        result = RuntimeServiceDefinition.from_row(row)
        if created:
            self._hub._cache_runtime_service(result, service)
        else:
            await self._hub.resolve_service(result.name)
        return result, created

    async def get(self, name: str) -> RuntimeServiceDefinition:
        row = await self._hub._store.get_service_definition(name)
        if row is None:
            raise NotFound("service", name)
        return RuntimeServiceDefinition.from_row(row)

    async def list(
        self,
        *,
        template: str | None = None,
        enabled: bool | None = None,
        limit: int = 100,
        after: str | None = None,
    ) -> Page[RuntimeServiceDefinition]:
        if not 1 <= limit <= 1000:
            raise ValidationFailed("limit: from 1 to 1000")
        rows = await self._hub._store.list_service_definitions(template=template, enabled=enabled)
        if after is not None:
            rows = [row for row in rows if row.name > after]
        page = rows[: limit + 1]
        next_cursor = page[limit - 1].name if len(page) > limit else None
        return Page([RuntimeServiceDefinition.from_row(row) for row in page[:limit]], next_cursor)

    async def update(
        self,
        name: str,
        *,
        title: str | _Unset = UNSET,
        url: str | _Unset | None = UNSET,
        template: str | _Unset = UNSET,
        config: Mapping[str, JsonValue] | _Unset = UNSET,
        expected_revision: int,
        actor: str,
    ) -> RuntimeServiceDefinition:
        self._actor(actor)
        current = await self.get(name)
        if current.revision != expected_revision:
            raise RevisionConflict("service", name, expected_revision, current.revision)
        spec = RuntimeServiceCreate(
            name=name,
            title=current.title if isinstance(title, _Unset) else title,
            url=current.url if isinstance(url, _Unset) else url,
            template=current.template if isinstance(template, _Unset) else template,
            config=current.config if isinstance(config, _Unset) else config,
        )
        candidate, service = await self._prepare_create(spec, enabled=current.enabled)
        changed_fields = tuple(
            field
            for field in ("title", "url", "template", "config")
            if getattr(candidate, field) != getattr(current, field)
        )
        if not changed_fields:
            return current
        row = await self._hub._store.update_service_definition(
            self._row(candidate),
            expected_revision=expected_revision,
            actor=actor,
            changed_fields=changed_fields,
        )
        result = RuntimeServiceDefinition.from_row(row)
        self._hub._cache_runtime_service(result, service)
        return result

    async def disable(
        self, name: str, *, expected_revision: int, actor: str, reason: str | None = None
    ) -> RuntimeServiceDefinition:
        self._actor(actor)
        return await self._set_enabled(
            name, False, expected_revision, actor=actor, reason=sanitize_text(reason)
        )

    async def enable(self, name: str, *, expected_revision: int, actor: str) -> RuntimeServiceDefinition:
        self._actor(actor)
        return await self._set_enabled(name, True, expected_revision, actor=actor, reason=None)

    async def _set_enabled(
        self,
        name: str,
        enabled: bool,
        expected_revision: int,
        *,
        actor: str,
        reason: str | None,
    ) -> RuntimeServiceDefinition:
        current = await self.get(name)
        if current.revision != expected_revision:
            raise RevisionConflict("service", name, expected_revision, current.revision)
        if current.enabled == enabled:
            return current
        service = self._hub._build_runtime_service(current) if enabled else None
        row = await self._hub._store.set_service_definition_enabled(
            name,
            enabled=enabled,
            expected_revision=expected_revision,
            actor=actor,
            reason=reason,
        )
        result = RuntimeServiceDefinition.from_row(row)
        if service is not None:
            self._hub._cache_runtime_service(result, service)
        else:
            self._hub._drop_runtime_service(name)
        return result

    async def _prepare_create(
        self, spec: RuntimeServiceCreate, *, enabled: bool = True
    ) -> tuple[RuntimeServiceDefinition, Service[Any, Any]]:
        if not isinstance(spec, RuntimeServiceCreate):
            raise ValidationFailed("spec: RuntimeServiceCreate required")
        if spec.name in self._hub._static_service_names:
            raise AlreadyExists("service", spec.name)
        template = self._hub._service_template(spec.template)
        config = self._validated_config(template, spec.config)
        now = await self._hub._store.current_time()
        definition = RuntimeServiceDefinition(
            spec.name, spec.title, spec.url, spec.template, config, enabled, 1, now, now
        )
        return definition, self._hub._build_runtime_service(definition)

    @staticmethod
    def _validated_config(template: ServiceTemplate, config: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        if not isinstance(config, Mapping):
            raise ValidationFailed("config: JSON object required")
        try:
            encoded = json.dumps(config, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ValidationFailed("config: JSON-compatible object required") from exc
        if len(encoded.encode()) > _MAX_CONFIG_BYTES:
            raise ValidationFailed(f"config: no more than {_MAX_CONFIG_BYTES // 1024} KiB")
        try:
            model = template.config_schema.model_validate(dict(config))
            value = model.model_dump(mode="json")
        except (ValidationError, ValueError, TypeError) as exc:
            errors = exc.errors() if isinstance(exc, ValidationError) else ()
            raise ValidationFailed("runtime service config is invalid", errors) from exc
        if not isinstance(value, dict):
            raise ValidationFailed("config schema must produce a JSON object")
        return value

    @staticmethod
    def _row(definition: RuntimeServiceDefinition) -> RuntimeServiceRow:
        return RuntimeServiceRow(
            definition.name, definition.title, definition.url, definition.template, dict(definition.config),
            definition.enabled, definition.revision, definition.created_at, definition.updated_at
        )

    @staticmethod
    def _actor(actor: str) -> None:
        if not isinstance(actor, str) or not actor.strip() or len(actor) > 256:
            raise ValidationFailed("actor: from 1 to 256 characters")

    @staticmethod
    def _canonical(spec: RuntimeServiceCreate) -> str:
        return json.dumps(
            {
                "name": spec.name,
                "title": spec.title,
                "url": spec.url,
                "template": spec.template,
                "config": spec.config,
            },
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )


def validate_template(template: ServiceTemplate) -> None:
    name = getattr(template, "name", None)
    schema = getattr(template, "config_schema", None)
    if not isinstance(name, str) or _TEMPLATE_NAME.fullmatch(name) is None:
        raise ValidationFailed("service template name is invalid")
    if not isinstance(schema, type) or not issubclass(schema, BaseModel):
        raise ValidationFailed(f"service template {name}: config_schema must be a Pydantic model")
    if not callable(getattr(template, "build", None)):
        raise ValidationFailed(f"service template {name}: build() is required")
