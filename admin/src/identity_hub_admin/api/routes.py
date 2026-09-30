"""Initial v1 routes used to prove the transport contract."""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from litestar import get
from litestar.openapi.datastructures import ResponseSpec
from litestar.params import FromPath

from identity_hub_admin.api.contracts import ApiMeta, ErrorEnvelope, ResourceEnvelope

AdminRole = Literal["viewer", "operator", "admin"]


class AdminRouteMeta(TypedDict):
    admin_roles: tuple[AdminRole, ...]
    auth_required: bool
    space_scoped: bool
    exclude_from_auth: bool


def route_meta(*roles: AdminRole, auth_required: bool = True, space_scoped: bool = False) -> dict[str, Any]:
    """Return required metadata for contract auditing and future guards."""
    metadata: AdminRouteMeta = {
        "admin_roles": roles,
        "auth_required": auth_required,
        "space_scoped": space_scoped,
        "exclude_from_auth": not auth_required,
    }
    return dict(metadata)


def error_responses(*statuses: int) -> dict[int, ResponseSpec]:
    return {
        status: ResponseSpec(
            data_container=ErrorEnvelope,
            description="Stable admin error envelope",
            generate_examples=False,
        )
        for status in statuses
    }


@get(
    "/api/v1/meta",
    operation_id="admin_v1_meta_get",
    tags=["meta"],
    opt=route_meta("viewer", "operator", "admin"),
    responses=error_responses(401, 403),
    sync_to_thread=False,
)
def api_meta() -> ResourceEnvelope[ApiMeta]:
    return ResourceEnvelope(data=ApiMeta())


@get(
    "/api/v1/spaces/{space_slug:str}/meta",
    operation_id="admin_v1_space_meta_get",
    tags=["meta"],
    opt=route_meta("viewer", "operator", "admin", space_scoped=True),
    responses=error_responses(401, 403, 404),
    sync_to_thread=False,
)
def space_meta(space_slug: FromPath[str]) -> ResourceEnvelope[ApiMeta]:
    return ResourceEnvelope(data=ApiMeta(space_slug=space_slug))


ROUTE_HANDLERS = [api_meta, space_meta]
