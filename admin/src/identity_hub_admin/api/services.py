"""Safe service registry projected from public Identity Hub declarations."""

from __future__ import annotations

from datetime import datetime

from identity_hub.read.services import ServiceDescriptor
from litestar import Request, get
from litestar.datastructures import State
from litestar.exceptions import HTTPException, NotFoundException
from litestar.params import FromPath

from identity_hub_admin.api.contracts import ContractModel, ResourceEnvelope
from identity_hub_admin.api.routes import error_responses, route_meta
from identity_hub_admin.auth import AdminPrincipal
from identity_hub_admin.runtime import AdminRuntime


class ServiceFieldResponse(ContractModel):
    name: str
    kind: str
    required: bool
    sensitive: bool
    filterable: bool
    searchable: bool


class ServiceSchemaResponse(ContractModel):
    category: str
    fields: list[ServiceFieldResponse]


class ServiceQuotaResponse(ContractModel):
    limit: int
    window_seconds: int
    origin: datetime


class ServiceOperationResponse(ContractModel):
    kind: str
    standing: list[str]
    needs_auth: bool
    verified: list[str]
    tiers: list[str]
    quotas: list[ServiceQuotaResponse]


class ServiceVerificationResponse(ContractModel):
    name: str
    requires: list[str]
    mode: str
    valid_for_seconds: int | None
    max_attempts: int | None
    reject_codes: list[str]
    evidence_required: bool
    pending_timeout_seconds: int | None


class ServiceTierResponse(ContractModel):
    name: str
    factors: list[str]


class ServiceCodeResponse(ContractModel):
    code: str
    standing: str


class ServiceProxyPolicyResponse(ContractModel):
    supported: bool
    pool: str | None
    sticky: bool | None
    max_identities: int | None
    on_proxy_down: str | None


class ServiceCapabilitiesResponse(ContractModel):
    authenticator: bool
    probes: bool
    error_classifier: bool
    importers: bool
    importer_count: int
    normalizers: list[str]
    profile_encrypted: bool
    lease_mode: str


class ServiceSpaceCountResponse(ContractModel):
    space_slug: str
    count: int


class ServiceDescriptorResponse(ContractModel):
    name: str
    title: str
    total: int
    spaces: list[ServiceSpaceCountResponse]
    schemas: list[ServiceSchemaResponse]
    operations: list[ServiceOperationResponse]
    verifications: list[ServiceVerificationResponse]
    tiers: list[ServiceTierResponse]
    standing_codes: list[ServiceCodeResponse]
    proxy_policy: ServiceProxyPolicyResponse
    capabilities: ServiceCapabilitiesResponse


class ServiceCatalogResponse(ContractModel):
    items: list[ServiceDescriptorResponse]


def _runtime(
    request: Request[AdminPrincipal, object, State], state: State
) -> tuple[AdminRuntime, AdminPrincipal]:
    principal = request.user
    if not isinstance(principal, AdminPrincipal):
        raise RuntimeError("authentication guard did not provide a principal")
    runtime: AdminRuntime = state.runtime
    if runtime.hub is None:
        raise HTTPException("Identity Hub is unavailable", status_code=503)
    return runtime, principal


def _response(item: ServiceDescriptor) -> ServiceDescriptorResponse:
    return ServiceDescriptorResponse.model_validate(item, from_attributes=True)


async def _catalog(
    request: Request[AdminPrincipal, object, State], state: State
) -> tuple[ServiceDescriptor, ...]:
    runtime, principal = _runtime(request, state)
    assert runtime.hub is not None
    return await runtime.hub.service_catalog(principal.allowed_spaces)


@get(
    "/api/v1/services",
    operation_id="admin_v1_services_get",
    tags=["services"],
    opt=route_meta("viewer", "operator", "admin"),
    responses=error_responses(401, 403, 503),
)
async def services(
    request: Request[AdminPrincipal, object, State], state: State
) -> ResourceEnvelope[ServiceCatalogResponse]:
    return ResourceEnvelope(
        data=ServiceCatalogResponse(items=[_response(item) for item in await _catalog(request, state)])
    )


@get(
    "/api/v1/services/{service_name:str}",
    operation_id="admin_v1_service_get",
    tags=["services"],
    opt=route_meta("viewer", "operator", "admin"),
    responses=error_responses(401, 403, 404, 503),
)
async def service_detail(
    request: Request[AdminPrincipal, object, State],
    state: State,
    service_name: FromPath[str],
) -> ResourceEnvelope[ServiceDescriptorResponse]:
    item = next((item for item in await _catalog(request, state) if item.name == service_name), None)
    if item is None:
        raise NotFoundException("service not found")
    return ResourceEnvelope(data=_response(item))


SERVICE_ROUTE_HANDLERS = [services, service_detail]
