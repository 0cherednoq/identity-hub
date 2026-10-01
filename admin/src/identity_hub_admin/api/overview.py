"""Operational overview endpoints backed only by public Identity Hub read models."""

from __future__ import annotations

from datetime import datetime

from identity_hub.errors import NotFound
from identity_hub.read.overview import ServiceOverview, SpaceOverview, SpacesMatrix
from litestar import Request, get
from litestar.datastructures import State
from litestar.exceptions import HTTPException, NotFoundException
from litestar.params import FromPath, Parameter

from identity_hub_admin.api.contracts import ContractModel, ResourceEnvelope
from identity_hub_admin.api.routes import error_responses, route_meta
from identity_hub_admin.auth import AdminPrincipal
from identity_hub_admin.runtime import AdminRuntime


class FactorCountResponse(ContractModel):
    factor: str
    states: dict[str, int]


class TierCountResponse(ContractModel):
    tier: str
    count: int


class RequirementCountResponse(ContractModel):
    code: str
    count: int
    next_deadline: datetime | None


class ServiceOverviewResponse(ContractModel):
    service: str
    title: str
    total: int
    eligible: int
    leased: int
    free_eligible: int
    heavy: int
    open_challenges: int
    action_required: int
    added_24h: int
    added_7d: int
    regions: dict[str, dict[str, int]]
    factors: list[FactorCountResponse]
    tiers: list[TierCountResponse]
    requirements: list[RequirementCountResponse]


class SpaceOverviewResponse(ContractModel):
    id: str
    slug: str
    title: str
    total: int
    eligible: int
    leased: int
    free_eligible: int
    heavy: int
    open_challenges: int
    action_required: int
    added_24h: int
    added_7d: int
    services: list[ServiceOverviewResponse]


class OverviewTotalsResponse(ContractModel):
    spaces: int
    total: int
    eligible: int
    leased: int
    free_eligible: int
    heavy: int
    open_challenges: int
    action_required: int


class SpacesOverviewResponse(ContractModel):
    totals: OverviewTotalsResponse
    spaces: list[SpaceOverviewResponse]
    service: str | None


def _service_response(value: ServiceOverview) -> ServiceOverviewResponse:
    return ServiceOverviewResponse(
        service=value.service,
        title=value.title,
        total=value.total,
        eligible=value.eligible,
        leased=value.leased,
        free_eligible=value.free_eligible,
        heavy=value.heavy,
        open_challenges=value.open_challenges,
        action_required=value.action_required,
        added_24h=value.added_24h,
        added_7d=value.added_7d,
        regions=value.regions,
        factors=[FactorCountResponse(factor=item.factor, states=item.states) for item in value.factors],
        tiers=[TierCountResponse(tier=item.tier, count=item.count) for item in value.tiers],
        requirements=[
            RequirementCountResponse(
                code=item.code,
                count=item.count,
                next_deadline=item.next_deadline,
            )
            for item in value.requirements
        ],
    )


def _space_response(value: SpaceOverview) -> SpaceOverviewResponse:
    return SpaceOverviewResponse(
        id=str(value.space.id),
        slug=value.space.slug,
        title=value.space.title,
        total=value.total,
        eligible=value.eligible,
        leased=value.leased,
        free_eligible=value.free_eligible,
        heavy=value.heavy,
        open_challenges=value.open_challenges,
        action_required=value.action_required,
        added_24h=value.added_24h,
        added_7d=value.added_7d,
        services=[_service_response(item) for item in value.services],
    )


def _matrix_response(value: SpacesMatrix) -> SpacesOverviewResponse:
    spaces = [_space_response(item) for item in value.spaces]

    def total(field: str) -> int:
        return sum(int(getattr(item, field)) for item in spaces)

    return SpacesOverviewResponse(
        totals=OverviewTotalsResponse(
            spaces=len(spaces),
            total=total("total"),
            eligible=total("eligible"),
            leased=total("leased"),
            free_eligible=total("free_eligible"),
            heavy=total("heavy"),
            open_challenges=total("open_challenges"),
            action_required=total("action_required"),
        ),
        spaces=spaces,
        service=value.service,
    )


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


async def _matrix(
    request: Request[AdminPrincipal, object, State], state: State, service: str | None
) -> SpacesMatrix:
    runtime, principal = _runtime(request, state)
    assert runtime.hub is not None
    if service is not None:
        try:
            runtime.hub.service(service)
        except NotFound:
            raise NotFoundException("service not found") from None
    return await runtime.hub.spaces_matrix(principal.allowed_spaces, service=service)


@get(
    "/api/v1/overview",
    operation_id="admin_v1_overview_get",
    tags=["overview"],
    opt=route_meta("viewer", "operator", "admin"),
    responses=error_responses(401, 403, 404, 503),
)
async def overview(
    request: Request[AdminPrincipal, object, State],
    state: State,
    service: str | None = Parameter(default=None, max_length=64),
) -> ResourceEnvelope[SpacesOverviewResponse]:
    return ResourceEnvelope(data=_matrix_response(await _matrix(request, state, service)))


@get(
    "/api/v1/spaces",
    operation_id="admin_v1_spaces_get",
    tags=["overview"],
    opt=route_meta("viewer", "operator", "admin"),
    responses=error_responses(401, 403, 404, 503),
)
async def spaces(
    request: Request[AdminPrincipal, object, State],
    state: State,
    service: str | None = Parameter(default=None, max_length=64),
) -> ResourceEnvelope[SpacesOverviewResponse]:
    return ResourceEnvelope(data=_matrix_response(await _matrix(request, state, service)))


@get(
    "/api/v1/spaces/{space_slug:str}/overview",
    operation_id="admin_v1_space_overview_get",
    tags=["overview"],
    opt=route_meta("viewer", "operator", "admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 503),
)
async def space_overview(
    request: Request[AdminPrincipal, object, State],
    space_slug: FromPath[str],
    service: str | None = Parameter(default=None, max_length=64),
) -> ResourceEnvelope[SpaceOverviewResponse]:
    handle = getattr(request.state, "space_handle", None)
    if handle is None or handle.slug != space_slug:
        raise NotFoundException("space not found")
    try:
        value = await handle.admin.overview(service=service)
    except NotFound:
        raise NotFoundException("service not found") from None
    return ResourceEnvelope(data=_space_response(value))


OVERVIEW_ROUTE_HANDLERS = [overview, spaces, space_overview]
