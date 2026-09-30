"""Deterministic, secret-free projections of registered service declarations."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from types import NoneType
from typing import TYPE_CHECKING, Any, get_args, get_origin

from pydantic import BaseModel, SecretBytes, SecretStr

if TYPE_CHECKING:
    from identity_hub.hub import IdentityHub
    from identity_hub.service import Service


@dataclass(frozen=True, slots=True)
class ServiceField:
    name: str
    kind: str
    required: bool
    sensitive: bool
    filterable: bool
    searchable: bool


@dataclass(frozen=True, slots=True)
class ServiceSchema:
    category: str
    fields: tuple[ServiceField, ...]


@dataclass(frozen=True, slots=True)
class ServiceQuota:
    limit: int
    window_seconds: int
    origin: datetime


@dataclass(frozen=True, slots=True)
class ServiceOperation:
    kind: str
    standing: tuple[str, ...]
    needs_auth: bool
    verified: tuple[str, ...]
    tiers: tuple[str, ...]
    quotas: tuple[ServiceQuota, ...]


@dataclass(frozen=True, slots=True)
class ServiceVerification:
    name: str
    requires: tuple[str, ...]
    mode: str
    valid_for_seconds: int | None
    max_attempts: int | None
    reject_codes: tuple[str, ...]
    evidence_required: bool
    pending_timeout_seconds: int | None


@dataclass(frozen=True, slots=True)
class ServiceTier:
    name: str
    factors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ServiceCode:
    code: str
    standing: str


@dataclass(frozen=True, slots=True)
class ServiceProxyPolicy:
    supported: bool
    pool: str | None
    sticky: bool | None
    max_identities: int | None
    on_proxy_down: str | None


@dataclass(frozen=True, slots=True)
class ServiceCapabilities:
    authenticator: bool
    probes: bool
    error_classifier: bool
    importers: bool
    importer_count: int
    normalizers: tuple[str, ...]
    profile_encrypted: bool
    lease_mode: str


@dataclass(frozen=True, slots=True)
class ServiceSpaceCount:
    space_slug: str
    count: int


@dataclass(frozen=True, slots=True)
class ServiceDescriptor:
    name: str
    title: str
    total: int
    spaces: tuple[ServiceSpaceCount, ...]
    schemas: tuple[ServiceSchema, ...]
    operations: tuple[ServiceOperation, ...]
    verifications: tuple[ServiceVerification, ...]
    tiers: tuple[ServiceTier, ...]
    standing_codes: tuple[ServiceCode, ...]
    proxy_policy: ServiceProxyPolicy
    capabilities: ServiceCapabilities


def _kind(annotation: Any) -> str:
    if annotation in {SecretStr, SecretBytes}:
        return "secret"
    args = tuple(arg for arg in get_args(annotation) if arg is not NoneType)
    if any(_kind(arg) == "secret" for arg in args):
        return "secret"
    origin = get_origin(annotation)
    if origin in {list, tuple, set, frozenset}:
        return "array"
    if origin is not None:
        if isinstance(origin, type) and issubclass(origin, Mapping):
            return "object"
        if len(args) == 1:
            return _kind(args[0])
    if annotation is str:
        return "string"
    if annotation is bool:
        return "boolean"
    if annotation is int:
        return "integer"
    if annotation is float:
        return "number"
    if annotation in {date, datetime}:
        return "datetime"
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return "object"
    return "value"


def _schemas(service: Service) -> tuple[ServiceSchema, ...]:
    values = []
    for category in ("attributes", "profile", "credentials", "session"):
        model = getattr(service, category)
        fields = tuple(
            ServiceField(
                name=name,
                kind=_kind(field.annotation),
                required=field.is_required(),
                sensitive=category in {"credentials", "session"} or _kind(field.annotation) == "secret",
                filterable=category == "profile" and name in service.filterable_profile,
                searchable=category == "profile" and name in service.searchable_profile,
            )
            for name, field in sorted(model.model_fields.items())
        )
        values.append(ServiceSchema(category, fields))
    return tuple(values)


def _descriptor(service: Service, counts: Mapping[str, int]) -> ServiceDescriptor:
    proxy = service.proxy_policy
    return ServiceDescriptor(
        name=service.name,
        title=service.title,
        total=sum(counts.values()),
        spaces=tuple(ServiceSpaceCount(slug, count) for slug, count in sorted(counts.items())),
        schemas=_schemas(service),
        operations=tuple(
            ServiceOperation(
                kind=kind,
                standing=tuple(sorted(str(value) for value in rule.standing)),
                needs_auth=rule.needs_auth,
                verified=tuple(sorted(rule.verified)),
                tiers=tuple(sorted(rule.tiers)),
                quotas=tuple(
                    ServiceQuota(quota.limit, quota.window_seconds, quota.origin)
                    for quota in sorted(rule.quotas, key=lambda item: (item.window_seconds, item.limit))
                ),
            )
            for kind, rule in sorted(service.operations.items())
        ),
        verifications=tuple(
            ServiceVerification(
                name=spec.name,
                requires=tuple(sorted(spec.requires)),
                mode=spec.mode.value,
                valid_for_seconds=(int(spec.valid_for.total_seconds()) if spec.valid_for else None),
                max_attempts=spec.max_attempts,
                reject_codes=tuple(sorted(spec.reject_codes)),
                evidence_required=spec.evidence is not None,
                pending_timeout_seconds=(
                    int(spec.pending_timeout.total_seconds()) if spec.pending_timeout else None
                ),
            )
            for spec in sorted(service.verifications, key=lambda item: item.name)
        ),
        tiers=tuple(
            ServiceTier(tier.name, tuple(sorted(tier.factors)))
            for tier in sorted(service.tiers, key=lambda item: item.name)
        ),
        standing_codes=tuple(
            ServiceCode(code, str(standing)) for code, standing in sorted(service.standing_codes.items())
        ),
        proxy_policy=ServiceProxyPolicy(
            supported=proxy is not None,
            pool=proxy.group if proxy else None,
            sticky=proxy.sticky if proxy else None,
            max_identities=proxy.max_identities if proxy else None,
            on_proxy_down=proxy.on_proxy_down.value if proxy else None,
        ),
        capabilities=ServiceCapabilities(
            authenticator=service.authenticator is not None,
            probes=service.probes is not None,
            error_classifier=service.error_classifier is not None,
            importers=bool(service.importers),
            importer_count=len(service.importers),
            normalizers=tuple(sorted(service.normalizers)),
            profile_encrypted=service.profile_encrypted,
            lease_mode=service.lease_mode.value,
        ),
    )


async def service_catalog(
    hub: IdentityHub,
    allowed_spaces: Collection[str] | None = None,
) -> tuple[ServiceDescriptor, ...]:
    matrix = await hub.spaces_matrix(allowed_spaces)
    counts = {
        service.name: {
            space.space.slug: next(
                (item.total for item in space.services if item.service == service.name),
                0,
            )
            for space in matrix.spaces
        }
        for service in hub.services
    }
    return tuple(
        _descriptor(service, counts[service.name]) for service in sorted(hub.services, key=lambda x: x.name)
    )
