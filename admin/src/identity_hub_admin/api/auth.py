"""Authentication session endpoints."""

from __future__ import annotations

import secrets

from litestar import Request, Response, get, post
from litestar.datastructures import State
from litestar.status_codes import HTTP_200_OK
from pydantic import Field

from identity_hub_admin.api.contracts import ContractModel, ResourceEnvelope
from identity_hub_admin.api.routes import error_responses, route_meta
from identity_hub_admin.auth import CSRF_COOKIE, SESSION_COOKIE, AdminPrincipal, AuthService


class LoginRequest(ContractModel):
    username: str
    password: str = Field(json_schema_extra={"writeOnly": True})


class PrincipalResponse(ContractModel):
    actor: str
    role: str
    allowed_spaces: list[str]


class LogoutResponse(ContractModel):
    logged_out: bool = True


def _principal_response(principal: AdminPrincipal) -> PrincipalResponse:
    return PrincipalResponse(
        actor=principal.actor,
        role=principal.role,
        allowed_spaces=sorted(principal.allowed_spaces),
    )


@post(
    "/api/v1/auth/login",
    operation_id="admin_v1_auth_login_post",
    tags=["auth"],
    opt=route_meta(auth_required=False),
    responses=error_responses(401, 429),
    status_code=HTTP_200_OK,
    sync_to_thread=True,
)
def login(
    data: LoginRequest, request: Request[object, object, State], state: State
) -> Response[ResourceEnvelope[PrincipalResponse]]:
    service: AuthService = state.auth_service
    source = request.client.host if request.client is not None else "unknown"
    service.check_login_rate(source)
    principal = service.authenticate_password(data.username, data.password)
    csrf_token = secrets.token_urlsafe(32)
    session = service.sessions.dumps(principal, csrf_token)
    cookie_path = service.settings.base_path or "/"
    response: Response[ResourceEnvelope[PrincipalResponse]] = Response(
        ResourceEnvelope(data=_principal_response(principal)),
        headers={"Cache-Control": "no-store"},
    )
    response.set_cookie(
        SESSION_COOKIE,
        session,
        max_age=service.sessions.ttl_seconds,
        path=cookie_path,
        secure=True,
        httponly=True,
        samesite="lax",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf_token,
        max_age=service.sessions.ttl_seconds,
        path=cookie_path,
        secure=True,
        httponly=False,
        samesite="lax",
    )
    return response


@post(
    "/api/v1/auth/logout",
    operation_id="admin_v1_auth_logout_post",
    tags=["auth"],
    opt=route_meta("viewer", "operator", "admin"),
    responses=error_responses(401, 403),
    status_code=HTTP_200_OK,
    sync_to_thread=False,
)
def logout(state: State) -> Response[ResourceEnvelope[LogoutResponse]]:
    service: AuthService = state.auth_service
    cookie_path = service.settings.base_path or "/"
    response: Response[ResourceEnvelope[LogoutResponse]] = Response(
        ResourceEnvelope(data=LogoutResponse()),
        headers={"Cache-Control": "no-store"},
    )
    response.delete_cookie(SESSION_COOKIE, path=cookie_path)
    response.delete_cookie(CSRF_COOKIE, path=cookie_path)
    return response


@get(
    "/api/v1/auth/me",
    operation_id="admin_v1_auth_me_get",
    tags=["auth"],
    opt=route_meta("viewer", "operator", "admin"),
    responses=error_responses(401, 403),
    sync_to_thread=False,
)
def me(request: Request[object, object, State]) -> ResourceEnvelope[PrincipalResponse]:
    principal = request.user
    if not isinstance(principal, AdminPrincipal):
        raise RuntimeError("authentication guard did not provide a principal")
    return ResourceEnvelope(data=_principal_response(principal))


AUTH_ROUTE_HANDLERS = [login, logout, me]
