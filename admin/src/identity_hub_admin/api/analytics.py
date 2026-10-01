"""Bounded, space-scoped analytics HTTP read models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, cast

from identity_hub.errors import ValidationFailed
from identity_hub.hub import SpaceHandle
from identity_hub.read.analytics import MAX_GROUPS, MAX_POINTS, MAX_RANGE, METRIC_DIMENSIONS
from identity_hub.rollups import BucketSize
from litestar import Request, Response, get
from litestar.exceptions import HTTPException, NotFoundException
from litestar.params import FromPath, FromQuery, Parameter

from identity_hub_admin.api.contracts import ContractModel, ResourceEnvelope
from identity_hub_admin.api.routes import error_responses, route_meta

_CACHE_CONTROL = "private, max-age=0, must-revalidate"

FORMULAS = {
    "series": "sum event or telemetry rollups per wall-clock bucket; absent buckets are zero",
    "funnel": "identities added in range, then first authenticated and tier-reached outcomes",
    "time_to": "seconds from first source event to first target event; linear median and p90",
    "cohorts": "share still in standing=good at day offset among observable cohort identities",
    "ban_rate": "identities ever banned / identities added, grouped by immutable intake attributes",
    "utilization": "lease/window overlap seconds / (current eligible identities * window seconds)",
    "proxy_quality": "proxy-attributed operation failures and proxy status transitions in range",
}


class AnalyticsMetricResponse(ContractModel):
    metric: str
    dimensions: list[str]


class AnalyticsCatalogResponse(ContractModel):
    timezone: str
    bucket_sizes: list[BucketSize]
    metrics: list[AnalyticsMetricResponse]
    formulas: dict[str, str]
    max_points: int
    max_groups: int
    max_range_days: int


class AnalyticsPointResponse(ContractModel):
    at: datetime
    value: float


class AnalyticsSeriesGroupResponse(ContractModel):
    key: str
    points: list[AnalyticsPointResponse]
    total: float
    previous_total: float


class AnalyticsSeriesResponse(ContractModel):
    metric: str
    bucket_size: BucketSize
    group_by: str | None
    groups: list[AnalyticsSeriesGroupResponse]
    coverage: Literal["complete", "partial"]
    cache_tag: str


class FunnelStageResponse(ContractModel):
    key: str
    count: int
    share: float


class FunnelResponse(ContractModel):
    stages: list[FunnelStageResponse]
    cache_tag: str


class TimeToResponse(ContractModel):
    metric: str
    samples: int
    median_seconds: float | None
    p90_seconds: float | None
    average_attempts: float | None
    cache_tag: str


class CohortCellResponse(ContractModel):
    offset_days: int
    eligible: int
    good: int
    share: float | None


class CohortRowResponse(ContractModel):
    cohort_start: datetime
    size: int
    cells: list[CohortCellResponse]


class CohortResponse(ContractModel):
    rows: list[CohortRowResponse]
    coverage: Literal["complete", "partial"]
    cache_tag: str


class RateItemResponse(ContractModel):
    key: str
    added: int
    banned: int
    rate: float


class BanRateResponse(ContractModel):
    group_by: str
    items: list[RateItemResponse]
    cache_tag: str


class HeatmapCellResponse(ContractModel):
    weekday: int
    hour: int
    leases: int
    operations: int
    failed: int


class UtilizationResponse(ContractModel):
    leased_seconds: float
    eligible_current: int
    utilization: float | None
    heatmap: list[HeatmapCellResponse]
    cache_tag: str


class ProxyQualityResponse(ContractModel):
    failures: int
    quarantined: int
    dead: int
    recovered: int
    cache_tag: str


def _handle(request: Request[Any, Any, Any], space_slug: str) -> SpaceHandle:
    handle = getattr(request.state, "space_handle", None)
    if handle is None or handle.slug != space_slug:
        raise NotFoundException("space not found")
    return cast(SpaceHandle, handle)


def _model[ModelT: ContractModel](model: type[ModelT], value: object) -> ModelT:
    return model.model_validate(value, from_attributes=True)


def _etag_matches(request: Request[Any, Any, Any], etag: str) -> bool:
    candidates = request.headers.get("if-none-match", "").split(",")
    return "*" in candidates or etag in {item.strip().removeprefix("W/") for item in candidates}


def _cached[ItemT: ContractModel](
    request: Request[Any, Any, Any], item: ItemT, cache_tag: str
) -> Response[ResourceEnvelope[ItemT] | None]:
    etag = f'"{cache_tag}"'
    headers = {"Cache-Control": _CACHE_CONTROL, "ETag": etag, "Vary": "Authorization, Cookie"}
    if _etag_matches(request, etag):
        return Response(None, status_code=304, headers=headers)
    return Response(ResourceEnvelope(data=item), headers=headers)


@get(
    "/api/v1/spaces/{space_slug:str}/analytics/catalog",
    operation_id="admin_v1_analytics_catalog_get",
    tags=["analytics"],
    opt=route_meta("viewer", "operator", "admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 503),
)
async def analytics_catalog(
    request: Request[Any, Any, Any], space_slug: FromPath[str]
) -> ResourceEnvelope[AnalyticsCatalogResponse]:
    space = await _handle(request, space_slug).ref()
    return ResourceEnvelope(
        data=AnalyticsCatalogResponse(
            timezone=space.timezone,
            bucket_sizes=list(BucketSize),
            metrics=[
                AnalyticsMetricResponse(metric=metric, dimensions=sorted(dimensions))
                for metric, dimensions in sorted(METRIC_DIMENSIONS.items())
            ],
            formulas=FORMULAS,
            max_points=MAX_POINTS,
            max_groups=MAX_GROUPS,
            max_range_days=MAX_RANGE.days,
        )
    )


@get(
    "/api/v1/spaces/{space_slug:str}/analytics/series",
    operation_id="admin_v1_analytics_series_get",
    tags=["analytics"],
    opt=route_meta("viewer", "operator", "admin", space_scoped=True),
    responses=error_responses(400, 401, 403, 404, 503),
)
async def analytics_series(
    request: Request[Any, Any, Any],
    space_slug: FromPath[str],
    start: FromQuery[datetime],
    end: FromQuery[datetime],
    bucket: FromQuery[BucketSize],
    metric: str = Parameter(min_length=1, max_length=128),
    group_by: str | None = Parameter(default=None, max_length=64),
    service: str | None = Parameter(default=None, max_length=64),
) -> Response[ResourceEnvelope[AnalyticsSeriesResponse] | None]:
    try:
        value = await _handle(request, space_slug).analytics.series(
            metric,
            start=start,
            end=end,
            bucket_size=bucket,
            group_by=group_by,
            dimensions={"service": service} if service is not None else None,
        )
    except ValidationFailed as exc:
        raise HTTPException(str(exc), status_code=400) from None
    item = _model(AnalyticsSeriesResponse, value)
    return _cached(request, item, value.cache_tag)


@get(
    "/api/v1/spaces/{space_slug:str}/analytics/funnel",
    operation_id="admin_v1_analytics_funnel_get",
    tags=["analytics"],
    opt=route_meta("viewer", "operator", "admin", space_scoped=True),
    responses=error_responses(400, 401, 403, 404, 503),
)
async def analytics_funnel(
    request: Request[Any, Any, Any],
    space_slug: FromPath[str],
    start: FromQuery[datetime],
    end: FromQuery[datetime],
    service: str | None = Parameter(default=None, max_length=64),
) -> Response[ResourceEnvelope[FunnelResponse] | None]:
    try:
        value = await _handle(request, space_slug).analytics.funnel(start=start, end=end, service=service)
    except ValidationFailed as exc:
        raise HTTPException(str(exc), status_code=400) from None
    item = _model(FunnelResponse, value)
    return _cached(request, item, value.cache_tag)


@get(
    "/api/v1/spaces/{space_slug:str}/analytics/time-to",
    operation_id="admin_v1_analytics_time_to_get",
    tags=["analytics"],
    opt=route_meta("viewer", "operator", "admin", space_scoped=True),
    responses=error_responses(400, 401, 403, 404, 503),
)
async def analytics_time_to(
    request: Request[Any, Any, Any],
    space_slug: FromPath[str],
    start: FromQuery[datetime],
    end: FromQuery[datetime],
    metric: str = Parameter(min_length=1, max_length=128),
    service: str | None = Parameter(default=None, max_length=64),
) -> Response[ResourceEnvelope[TimeToResponse] | None]:
    try:
        value = await _handle(request, space_slug).analytics.time_to(
            metric, start=start, end=end, service=service
        )
    except ValidationFailed as exc:
        raise HTTPException(str(exc), status_code=400) from None
    item = _model(TimeToResponse, value)
    return _cached(request, item, value.cache_tag)


@get(
    "/api/v1/spaces/{space_slug:str}/analytics/cohorts",
    operation_id="admin_v1_analytics_cohorts_get",
    tags=["analytics"],
    opt=route_meta("viewer", "operator", "admin", space_scoped=True),
    responses=error_responses(400, 401, 403, 404, 503),
)
async def analytics_cohorts(
    request: Request[Any, Any, Any],
    space_slug: FromPath[str],
    start: FromQuery[datetime],
    end: FromQuery[datetime],
    service: str | None = Parameter(default=None, max_length=64),
) -> Response[ResourceEnvelope[CohortResponse] | None]:
    try:
        value = await _handle(request, space_slug).analytics.cohorts(start=start, end=end, service=service)
    except ValidationFailed as exc:
        raise HTTPException(str(exc), status_code=400) from None
    item = _model(CohortResponse, value)
    return _cached(request, item, value.cache_tag)


@get(
    "/api/v1/spaces/{space_slug:str}/analytics/ban-rate",
    operation_id="admin_v1_analytics_ban_rate_get",
    tags=["analytics"],
    opt=route_meta("viewer", "operator", "admin", space_scoped=True),
    responses=error_responses(400, 401, 403, 404, 503),
)
async def analytics_ban_rate(
    request: Request[Any, Any, Any],
    space_slug: FromPath[str],
    start: FromQuery[datetime],
    end: FromQuery[datetime],
    group_by: FromQuery[Literal["service", "batch", "tag", "proxy_pool"]],
    service: str | None = Parameter(default=None, max_length=64),
) -> Response[ResourceEnvelope[BanRateResponse] | None]:
    try:
        value = await _handle(request, space_slug).analytics.ban_rate(
            start=start, end=end, group_by=group_by, service=service
        )
    except ValidationFailed as exc:
        raise HTTPException(str(exc), status_code=400) from None
    item = _model(BanRateResponse, value)
    return _cached(request, item, value.cache_tag)


@get(
    "/api/v1/spaces/{space_slug:str}/analytics/utilization",
    operation_id="admin_v1_analytics_utilization_get",
    tags=["analytics"],
    opt=route_meta("viewer", "operator", "admin", space_scoped=True),
    responses=error_responses(400, 401, 403, 404, 503),
)
async def analytics_utilization(
    request: Request[Any, Any, Any],
    space_slug: FromPath[str],
    start: FromQuery[datetime],
    end: FromQuery[datetime],
    service: str | None = Parameter(default=None, max_length=64),
) -> Response[ResourceEnvelope[UtilizationResponse] | None]:
    try:
        value = await _handle(request, space_slug).analytics.utilization(
            start=start, end=end, service=service
        )
    except ValidationFailed as exc:
        raise HTTPException(str(exc), status_code=400) from None
    item = _model(UtilizationResponse, value)
    return _cached(request, item, value.cache_tag)


@get(
    "/api/v1/spaces/{space_slug:str}/analytics/proxy-quality",
    operation_id="admin_v1_analytics_proxy_quality_get",
    tags=["analytics"],
    opt=route_meta("viewer", "operator", "admin", space_scoped=True),
    responses=error_responses(400, 401, 403, 404, 503),
)
async def analytics_proxy_quality(
    request: Request[Any, Any, Any],
    space_slug: FromPath[str],
    start: FromQuery[datetime],
    end: FromQuery[datetime],
) -> Response[ResourceEnvelope[ProxyQualityResponse] | None]:
    try:
        value = await _handle(request, space_slug).analytics.proxy_quality(start=start, end=end)
    except ValidationFailed as exc:
        raise HTTPException(str(exc), status_code=400) from None
    item = _model(ProxyQualityResponse, value)
    return _cached(request, item, value.cache_tag)


ANALYTICS_ROUTE_HANDLERS = [
    analytics_catalog,
    analytics_series,
    analytics_funnel,
    analytics_time_to,
    analytics_cohorts,
    analytics_ban_rate,
    analytics_utilization,
    analytics_proxy_quality,
]
