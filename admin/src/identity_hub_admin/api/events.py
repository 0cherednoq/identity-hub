"""Space-scoped event journal with catalog filters and redacted details."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, cast
from uuid import UUID

from identity_hub.errors import NotFound, ValidationFailed
from identity_hub.events import CATALOG
from identity_hub.hub import SpaceHandle
from identity_hub.read.events import EventFilters, event_groups
from litestar import Request, get
from litestar.exceptions import HTTPException, NotFoundException
from litestar.params import FromPath, FromQuery, Parameter

from identity_hub_admin.api.contracts import (
    ContractModel,
    CursorPayload,
    ResourceEnvelope,
    decode_cursor,
    encode_cursor,
)
from identity_hub_admin.api.routes import error_responses, route_meta


class EventTypeResponse(ContractModel):
    type: str
    group: str
    external: bool
    default_severity: str


class EventItemResponse(ContractModel):
    event_id: UUID
    type: str
    group: str
    severity: str
    service: str | None
    identity_id: UUID | None
    proxy_id: UUID | None
    lease_id: UUID | None
    sequence: int | None
    actor: str
    summary: str
    data: dict[str, Any]
    external: bool
    occurred_at: datetime


class EventPageResponse(ContractModel):
    items: list[EventItemResponse]
    next_cursor: str | None
    groups: list[str]
    types: list[EventTypeResponse]


def _handle(request: Request[Any, Any, Any], space_slug: str) -> SpaceHandle:
    handle = getattr(request.state, "space_handle", None)
    if handle is None or handle.slug != space_slug:
        raise NotFoundException("space not found")
    return cast(SpaceHandle, handle)


def _cursor(value: str | None) -> tuple[datetime, UUID] | None:
    if value is None:
        return None
    try:
        payload = decode_cursor(value)
        if payload.sort != "occurred_at":
            raise ValueError("invalid event cursor sort")
        occurred_at = datetime.fromisoformat(payload.value)
        if occurred_at.tzinfo is None:
            raise ValueError("event cursor datetime must include timezone")
        return occurred_at, payload.id
    except ValueError as exc:
        raise HTTPException(str(exc), status_code=400) from None


def _next_cursor(value: tuple[datetime, UUID] | None) -> str | None:
    if value is None:
        return None
    return encode_cursor(CursorPayload(sort="occurred_at", value=value[0].isoformat(), id=value[1]))


def _item(value: object) -> EventItemResponse:
    return EventItemResponse.model_validate(value, from_attributes=True)


@get(
    "/api/v1/spaces/{space_slug:str}/events",
    operation_id="admin_v1_events_get",
    tags=["events"],
    opt=route_meta("viewer", "operator", "admin", space_scoped=True),
    responses=error_responses(400, 401, 403, 404, 503),
)
async def events_page(
    request: Request[Any, Any, Any],
    space_slug: FromPath[str],
    type: FromQuery[str | None] = None,
    group: str | None = Parameter(default=None, max_length=64),
    severity: FromQuery[Literal["info", "warning", "critical"] | None] = None,
    service: str | None = Parameter(default=None, max_length=64),
    identity_id: FromQuery[UUID | None] = None,
    proxy_id: FromQuery[UUID | None] = None,
    lease_id: FromQuery[UUID | None] = None,
    actor: str | None = Parameter(default=None, max_length=256),
    occurred_from: FromQuery[datetime | None] = None,
    occurred_to: FromQuery[datetime | None] = None,
    include_journal: FromQuery[bool] = False,
    cursor: str | None = Parameter(default=None, max_length=2048),
    limit: int = Parameter(default=50, ge=1, le=200),
) -> ResourceEnvelope[EventPageResponse]:
    try:
        page = await _handle(request, space_slug).event_page(
            filters=EventFilters(
                type=type,
                group=group,
                severity=severity,
                service=service,
                identity_id=identity_id,
                proxy_id=proxy_id,
                lease_id=lease_id,
                actor=actor,
                occurred_from=occurred_from,
                occurred_to=occurred_to,
                include_journal=include_journal,
            ),
            limit=limit,
            before=_cursor(cursor),
        )
    except ValidationFailed as exc:
        raise HTTPException(str(exc), status_code=400) from None
    return ResourceEnvelope(
        data=EventPageResponse(
            items=[_item(item) for item in page.items],
            next_cursor=_next_cursor(page.next_before),
            groups=list(event_groups()),
            types=[
                EventTypeResponse(
                    type=event_type,
                    group=spec.group,
                    external=spec.external,
                    default_severity=spec.severity.value,
                )
                for event_type, spec in sorted(CATALOG.items())
            ],
        )
    )


@get(
    "/api/v1/spaces/{space_slug:str}/events/{event_id:uuid}",
    operation_id="admin_v1_event_get",
    tags=["events"],
    opt=route_meta("viewer", "operator", "admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 503),
)
async def event_detail(
    request: Request[Any, Any, Any],
    space_slug: FromPath[str],
    event_id: FromPath[UUID],
) -> ResourceEnvelope[EventItemResponse]:
    try:
        item = await _handle(request, space_slug).event_detail(event_id)
    except NotFound:
        raise NotFoundException("event not found") from None
    return ResourceEnvelope(data=_item(item))


EVENT_ROUTE_HANDLERS = [events_page, event_detail]
