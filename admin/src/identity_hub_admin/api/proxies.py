"""Space-scoped, secret-free proxy overview and detail endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, cast
from uuid import UUID

from identity_hub.errors import NotFound, ValidationFailed
from identity_hub.hub import SpaceHandle
from identity_hub.read.proxies import ProxyCapacity, ProxyFilters
from litestar import Request, get
from litestar.exceptions import HTTPException, NotFoundException
from litestar.params import FromPath, FromQuery, Parameter
from pydantic import Field

from identity_hub_admin.api.contracts import (
    ContractModel,
    CursorPayload,
    ResourceEnvelope,
    decode_cursor,
    encode_cursor,
)
from identity_hub_admin.api.routes import error_responses, route_meta
from identity_hub_admin.auth import AdminPrincipal, AdminRole


class ProxyCardResponse(ContractModel):
    id: UUID
    pool: str
    scheme: str
    host: str
    port: int
    masked_url: str
    status: str
    error_score: float
    fail_streak: int
    next_check_at: datetime | None
    status_changed_at: datetime
    status_source: str
    checked_at: datetime | None
    pinned_until: datetime | None
    pending_observation: str | None
    max_identities: int
    bound_count: int
    available_capacity: int
    capacity_utilization: float
    in_lease_count: int
    created_at: datetime
    actions_available: bool
    actions_unavailable_reason: str


class ProxyPoolCapacityResponse(ContractModel):
    pool: str
    proxies: int
    alive: int
    quarantined: int
    dead: int
    other: int
    max_identities: int
    bound_count: int
    available_capacity: int
    in_lease_count: int
    utilization: float


class ProxyPageResponse(ContractModel):
    items: list[ProxyCardResponse]
    pools: list[ProxyPoolCapacityResponse]
    next_cursor: str | None


class ProxyBoundIdentityResponse(ContractModel):
    identity_id: UUID
    service: str
    external_ref: str
    bound_at: datetime


class ProxyBindingHistoryResponse(ContractModel):
    event_id: UUID
    identity_id: UUID | None
    service: str | None
    external_ref: str | None
    from_proxy_id: UUID | None
    to_proxy_id: UUID | None
    reason: str
    pool: str
    actor: str
    occurred_at: datetime


class ProxyActionResponse(ContractModel):
    id: str
    kind: Literal["recheck", "override"]
    label: str
    required_role: Literal["operator", "admin"]
    allowed_targets: list[str] = Field(default_factory=list)
    requires_confirmation: bool = False
    consequence_codes: list[str] = Field(default_factory=list)


class ProxyDetailResponse(ContractModel):
    proxy: ProxyCardResponse
    bound_identities: list[ProxyBoundIdentityResponse]
    history: list[ProxyBindingHistoryResponse]
    next_history_cursor: str | None
    actions: list[ProxyActionResponse] = Field(default_factory=list)


def _actions(request: Request[Any, Any, Any]) -> list[ProxyActionResponse]:
    role = request.user.role
    if role == AdminRole.VIEWER:
        return []
    actions: list[ProxyActionResponse] = []
    if getattr(request.app.state, "proxy_checker", None) is not None:
        actions.append(
            ProxyActionResponse(
                id="proxy:recheck", kind="recheck", label="Проверить сейчас", required_role="operator"
            )
        )
    if role == AdminRole.ADMIN:
        actions.append(
            ProxyActionResponse(
                id="proxy:override",
                kind="override",
                label="Исправить статус вручную",
                required_role="admin",
                allowed_targets=["alive", "quarantined", "dead"],
                requires_confirmation=True,
                consequence_codes=["overrides_proxy_health"],
            )
        )
    return actions


def _card_response(item: Any, actions: list[ProxyActionResponse]) -> ProxyCardResponse:
    reason = "" if actions else "Для действий нужна роль operator или настроенный checker."
    return ProxyCardResponse.model_validate(item, from_attributes=True).model_copy(
        update={"actions_available": bool(actions), "actions_unavailable_reason": reason}
    )


def _handle(request: Request[Any, Any, Any], space_slug: str) -> SpaceHandle:
    handle = getattr(request.state, "space_handle", None)
    if handle is None or handle.slug != space_slug:
        raise NotFoundException("space not found")
    return cast(SpaceHandle, handle)


def _cursor(value: str | None, sort: Literal["created_at", "occurred_at"]) -> tuple[datetime, UUID] | None:
    if value is None:
        return None
    try:
        payload = decode_cursor(value)
        if payload.sort != sort:
            raise ValueError("invalid proxy cursor sort")
        at = datetime.fromisoformat(payload.value)
        if at.tzinfo is None:
            raise ValueError("proxy cursor datetime must include timezone")
        return at, payload.id
    except ValueError as exc:
        raise HTTPException(str(exc), status_code=400) from None


def _next_cursor(
    value: tuple[datetime, UUID] | None, sort: Literal["created_at", "occurred_at"]
) -> str | None:
    if value is None:
        return None
    return encode_cursor(CursorPayload(sort=sort, value=value[0].isoformat(), id=value[1]))


@get(
    "/api/v1/spaces/{space_slug:str}/proxies",
    operation_id="admin_v1_proxies_get",
    tags=["proxies"],
    opt=route_meta("viewer", "operator", "admin", space_scoped=True),
    responses=error_responses(400, 401, 403, 404, 503),
)
async def proxies_overview(
    request: Request[AdminPrincipal, Any, Any],
    space_slug: FromPath[str],
    pool: str | None = Parameter(default=None, max_length=128),
    status: FromQuery[Literal["alive", "quarantined", "dead", "unknown"] | None] = None,
    capacity: FromQuery[ProxyCapacity | None] = None,
    cursor: str | None = Parameter(default=None, max_length=2048),
    limit: int = Parameter(default=50, ge=1, le=200),
) -> ResourceEnvelope[ProxyPageResponse]:
    try:
        page = await _handle(request, space_slug).admin.proxy_overview(
            filters=ProxyFilters(pool=pool, status=status, capacity=capacity),
            limit=limit,
            before=_cursor(cursor, "created_at"),
        )
    except ValidationFailed as exc:
        raise HTTPException(str(exc), status_code=400) from None
    return ResourceEnvelope(
        data=ProxyPageResponse(
            items=[_card_response(item, _actions(request)) for item in page.items],
            pools=[
                ProxyPoolCapacityResponse.model_validate(item, from_attributes=True) for item in page.pools
            ],
            next_cursor=_next_cursor(page.next_before, "created_at"),
        )
    )


@get(
    "/api/v1/spaces/{space_slug:str}/proxies/{proxy_id:uuid}",
    operation_id="admin_v1_proxy_get",
    tags=["proxies"],
    opt=route_meta("viewer", "operator", "admin", space_scoped=True),
    responses=error_responses(400, 401, 403, 404, 503),
)
async def proxy_detail(
    request: Request[AdminPrincipal, Any, Any],
    space_slug: FromPath[str],
    proxy_id: FromPath[UUID],
    history_cursor: str | None = Parameter(default=None, max_length=2048),
    history_limit: int = Parameter(default=50, ge=1, le=200),
) -> ResourceEnvelope[ProxyDetailResponse]:
    try:
        detail = await _handle(request, space_slug).admin.proxy_detail(
            proxy_id,
            history_limit=history_limit,
            history_before=_cursor(history_cursor, "occurred_at"),
        )
    except NotFound:
        raise NotFoundException("proxy not found") from None
    except ValidationFailed as exc:
        raise HTTPException(str(exc), status_code=400) from None
    return ResourceEnvelope(
        data=ProxyDetailResponse(
            proxy=_card_response(detail.proxy, _actions(request)),
            bound_identities=[
                ProxyBoundIdentityResponse.model_validate(item, from_attributes=True)
                for item in detail.bound_identities
            ],
            history=[
                ProxyBindingHistoryResponse.model_validate(item, from_attributes=True)
                for item in detail.history.items
            ],
            next_history_cursor=_next_cursor(detail.history.next_before, "occurred_at"),
            actions=_actions(request),
        )
    )


PROXY_ROUTE_HANDLERS = [proxies_overview, proxy_detail]
