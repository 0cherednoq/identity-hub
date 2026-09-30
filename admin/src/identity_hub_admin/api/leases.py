"""Operational, secret-free lease dashboard endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, cast
from uuid import UUID

from identity_hub.errors import ValidationFailed
from identity_hub.hub import SpaceHandle
from identity_hub.read.leases import LeaseFilters, LeaseGroup
from identity_hub.store.base import LeaseReadRow
from litestar import Request, get
from litestar.exceptions import HTTPException, NotFoundException
from litestar.params import FromPath, Parameter

from identity_hub_admin.api.contracts import (
    ContractModel,
    CursorPayload,
    ResourceEnvelope,
    decode_cursor,
    encode_cursor,
)
from identity_hub_admin.api.routes import error_responses, route_meta


class LeaseWindowResponse(ContractModel):
    window: str
    window_seconds: int
    acquired: int
    completed: int
    unique_identities: int
    average_seconds: float | None
    p95_seconds: float | None
    leased_seconds: float
    utilization: float | None
    eligible_identities: int
    end_reasons: dict[str, int]
    operations: dict[str, int]
    succeeded: int
    failed: int
    failure_rate: float | None
    empty_picks: int | None
    wait_p95_seconds: float | None


class LeaseSliceResponse(ContractModel):
    value: str
    metrics: LeaseWindowResponse
    loss_above_average: bool


class LeaseChartBucketResponse(ContractModel):
    start: datetime
    end: datetime
    acquired: int
    lost_or_expired: int
    empty_picks: int | None


class LiveLeaseResponse(ContractModel):
    lease_id: UUID
    identity_id: UUID
    service: str
    external_ref: str
    pool: str
    holder: str
    proxy_id: UUID | None
    acquired_at: datetime
    last_heartbeat_at: datetime
    heartbeat_lag_seconds: float
    heartbeat_stale: bool
    expires_at: datetime | None
    operations: dict[str, int]
    succeeded: int
    failed: int
    expired_unclosed: bool


class LeaseDashboardResponse(ContractModel):
    generated_at: datetime
    filters: dict[str, str | None]
    group_by: LeaseGroup
    windows: list[LeaseWindowResponse]
    slices: list[LeaseSliceResponse]
    chart: list[LeaseChartBucketResponse]
    live: list[LiveLeaseResponse]
    expired_unclosed: list[LiveLeaseResponse]
    empty_pick_metrics_available: bool
    operation_timestamps_available: bool


class LeaseHistoryItemResponse(ContractModel):
    lease_id: UUID
    identity_id: UUID
    service: str
    external_ref: str
    fencing: int
    pool: str
    holder: str
    proxy_id: UUID | None
    acquired_at: datetime
    last_heartbeat_at: datetime
    released_at: datetime | None
    duration_seconds: float | None
    end_reason: str | None
    operations: dict[str, int]
    succeeded: int
    failed: int
    last_error: str | None


class LeaseHistoryPageResponse(ContractModel):
    items: list[LeaseHistoryItemResponse]
    next_cursor: str | None


def _handle(request: Request[Any, Any, Any], space_slug: str) -> SpaceHandle:
    handle = getattr(request.state, "space_handle", None)
    if handle is None or handle.slug != space_slug:
        raise NotFoundException("space not found")
    return cast(SpaceHandle, handle)


def _filters(pool: str | None, service: str | None, holder: str | None) -> LeaseFilters:
    return LeaseFilters(pool=pool, service=service, holder=holder)


def _history_item(row: LeaseReadRow) -> LeaseHistoryItemResponse:
    return LeaseHistoryItemResponse(
        lease_id=row.lease_id,
        identity_id=row.identity_id,
        service=row.service,
        external_ref=row.external_ref,
        fencing=row.fencing,
        pool=row.pool,
        holder=row.holder,
        proxy_id=row.proxy_id,
        acquired_at=row.acquired_at,
        last_heartbeat_at=row.last_heartbeat_at,
        released_at=row.released_at,
        duration_seconds=row.duration.total_seconds() if row.duration else None,
        end_reason=row.end_reason,
        operations=dict(row.operations),
        succeeded=row.succeeded,
        failed=row.failed,
        last_error=row.last_error,
    )


@get(
    "/api/v1/spaces/{space_slug:str}/leases",
    operation_id="admin_v1_leases_get",
    tags=["leases"],
    opt=route_meta("viewer", "operator", "admin", space_scoped=True),
    responses=error_responses(400, 401, 403, 404, 503),
)
async def leases_dashboard(
    request: Request[Any, Any, Any],
    space_slug: FromPath[str],
    group_by: Literal["pool", "service", "holder"] = Parameter(default="pool"),
    pool: str | None = Parameter(default=None, max_length=128),
    service: str | None = Parameter(default=None, max_length=64),
    holder: str | None = Parameter(default=None, max_length=255),
) -> ResourceEnvelope[LeaseDashboardResponse]:
    try:
        dashboard = await _handle(request, space_slug).lease_dashboard(
            filters=_filters(pool, service, holder),
            group_by=group_by,
        )
    except ValidationFailed as exc:
        raise HTTPException(str(exc), status_code=400) from None
    return ResourceEnvelope(
        data=LeaseDashboardResponse.model_validate(
            {
                "generated_at": dashboard.generated_at,
                "filters": {
                    "pool": dashboard.filters.pool,
                    "service": dashboard.filters.service,
                    "holder": dashboard.filters.holder,
                },
                "group_by": dashboard.group_by,
                "windows": dashboard.windows,
                "slices": dashboard.slices,
                "chart": dashboard.chart,
                "live": dashboard.live,
                "expired_unclosed": dashboard.expired_unclosed,
                "empty_pick_metrics_available": dashboard.empty_pick_metrics_available,
                "operation_timestamps_available": dashboard.operation_timestamps_available,
            },
            from_attributes=True,
        )
    )


@get(
    "/api/v1/spaces/{space_slug:str}/leases/history",
    operation_id="admin_v1_leases_history_get",
    tags=["leases"],
    opt=route_meta("viewer", "operator", "admin", space_scoped=True),
    responses=error_responses(400, 401, 403, 404, 503),
)
async def leases_history(
    request: Request[Any, Any, Any],
    space_slug: FromPath[str],
    cursor: str | None = Parameter(default=None, max_length=2048),
    limit: int = Parameter(default=50, ge=1, le=200),
    pool: str | None = Parameter(default=None, max_length=128),
    service: str | None = Parameter(default=None, max_length=64),
    holder: str | None = Parameter(default=None, max_length=255),
) -> ResourceEnvelope[LeaseHistoryPageResponse]:
    before = None
    if cursor is not None:
        try:
            payload = decode_cursor(cursor)
            if payload.sort != "acquired_at":
                raise ValueError("invalid lease cursor sort")
            acquired_at = datetime.fromisoformat(payload.value)
            if acquired_at.tzinfo is None:
                raise ValueError("lease cursor datetime must include timezone")
            before = (acquired_at, payload.id)
        except ValueError as exc:
            raise HTTPException(str(exc), status_code=400) from None
    try:
        page = await _handle(request, space_slug).lease_history_page(
            limit=limit,
            before=before,
            filters=_filters(pool, service, holder),
        )
    except ValidationFailed as exc:
        raise HTTPException(str(exc), status_code=400) from None
    next_cursor = (
        encode_cursor(
            CursorPayload(sort="acquired_at", value=page.next_before[0].isoformat(), id=page.next_before[1])
        )
        if page.next_before is not None
        else None
    )
    return ResourceEnvelope(
        data=LeaseHistoryPageResponse(
            items=[_history_item(item) for item in page.items],
            next_cursor=next_cursor,
        )
    )


LEASE_ROUTE_HANDLERS = [leases_dashboard, leases_history]
