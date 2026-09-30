"""Secret-free, space-scoped queue for facts that require human attention."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, cast
from uuid import UUID

from identity_hub.errors import ValidationFailed
from identity_hub.hub import SpaceHandle
from identity_hub.read.human_queue import QueueKind, QueueUrgency
from litestar import Request, get
from litestar.exceptions import HTTPException, NotFoundException
from litestar.params import FromPath, FromQuery, Parameter

from identity_hub_admin.api.contracts import ContractModel, ResourceEnvelope
from identity_hub_admin.api.routes import error_responses, route_meta


class HumanQueueItemResponse(ContractModel):
    id: str
    kind: QueueKind
    identity_id: UUID
    service: str
    external_ref: str
    key: str
    reason: str
    urgency: QueueUrgency
    deadline: datetime | None
    expired: bool
    occurred_at: datetime
    action_tab: Literal["state", "verification"]
    action_available: bool
    action_unavailable_reason: str
    details: dict[str, str | int | float | bool | None]


class HumanQueuePageResponse(ContractModel):
    items: list[HumanQueueItemResponse]
    next_cursor: str | None
    counts: dict[str, int]


def _handle(request: Request[Any, Any, Any], space_slug: str) -> SpaceHandle:
    handle = getattr(request.state, "space_handle", None)
    if handle is None or handle.slug != space_slug:
        raise NotFoundException("space not found")
    return cast(SpaceHandle, handle)


@get(
    "/api/v1/spaces/{space_slug:str}/human-queue",
    operation_id="admin_v1_human_queue_get",
    tags=["human-queue"],
    opt=route_meta("viewer", "operator", "admin", space_scoped=True),
    responses=error_responses(400, 401, 403, 404, 503),
)
async def human_queue(
    request: Request[Any, Any, Any],
    space_slug: FromPath[str],
    kind: FromQuery[QueueKind | None] = None,
    service: str | None = Parameter(default=None, max_length=64),
    urgency: FromQuery[QueueUrgency | None] = None,
    cursor: str | None = Parameter(default=None, max_length=2048),
    limit: int = Parameter(default=50, ge=1, le=200),
) -> ResourceEnvelope[HumanQueuePageResponse]:
    try:
        page = await _handle(request, space_slug).human_queue(
            kind=kind,
            service=service,
            urgency=urgency,
            limit=limit,
            after=cursor,
        )
    except ValidationFailed as exc:
        raise HTTPException(str(exc), status_code=400) from None
    return ResourceEnvelope(
        data=HumanQueuePageResponse(
            items=[HumanQueueItemResponse.model_validate(item, from_attributes=True) for item in page.items],
            next_cursor=page.next_cursor,
            counts=page.counts,
        )
    )


HUMAN_QUEUE_ROUTE_HANDLERS = [human_queue]
