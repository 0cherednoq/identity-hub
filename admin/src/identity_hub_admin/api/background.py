"""Read-only health diagnostics for background workers and delivery queues."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from litestar import Request, get
from litestar.datastructures import State
from litestar.exceptions import HTTPException

from identity_hub_admin.api.contracts import ContractModel, ResourceEnvelope
from identity_hub_admin.api.routes import error_responses, route_meta
from identity_hub_admin.auth import AdminPrincipal
from identity_hub_admin.runtime import AdminRuntime


class BackgroundProcessResponse(ContractModel):
    name: str
    kind: str
    status: Literal["healthy", "delayed", "retrying", "stopped"]
    heartbeat_available: bool
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    lag_events: int | None
    lag_seconds: float | None
    backlog: int | None
    failed: int | None
    next_attempt_at: datetime | None
    last_error: str | None
    runbook_hint: str


class BackgroundSnapshotResponse(ContractModel):
    generated_at: datetime
    processes: list[BackgroundProcessResponse]


@get(
    "/api/v1/background",
    operation_id="admin_v1_background_get",
    tags=["background"],
    opt=route_meta("viewer", "operator", "admin"),
    responses=error_responses(401, 403, 503),
)
async def background(
    request: Request[AdminPrincipal, object, State], state: State
) -> ResourceEnvelope[BackgroundSnapshotResponse]:
    principal = request.user
    if not isinstance(principal, AdminPrincipal):
        raise RuntimeError("authentication guard did not provide a principal")
    runtime: AdminRuntime = state.runtime
    if runtime.hub is None:
        raise HTTPException("Identity Hub is unavailable", status_code=503)
    snapshot = await runtime.hub.background_snapshot(principal.allowed_spaces)
    return ResourceEnvelope(
        data=BackgroundSnapshotResponse(
            generated_at=snapshot.generated_at,
            processes=[
                BackgroundProcessResponse.model_validate(item, from_attributes=True)
                for item in snapshot.processes
            ],
        )
    )


BACKGROUND_ROUTE_HANDLERS = [background]
