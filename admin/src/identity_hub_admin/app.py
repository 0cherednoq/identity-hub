"""Litestar application factory.

The complete lifecycle and static delivery are implemented in A1.1. This module provides the
small typed application boundary required by the workspace scaffold.
"""

from typing import TYPE_CHECKING, Any

from identity_hub.hub import IdentityHub
from litestar import Litestar, Request, Response, get
from litestar.datastructures import State
from litestar.exceptions import HTTPException
from litestar.middleware import DefineMiddleware
from litestar.openapi import OpenAPIConfig
from litestar.status_codes import HTTP_200_OK, HTTP_503_SERVICE_UNAVAILABLE

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

from identity_hub_admin import __version__
from identity_hub_admin.api.account_commands import ACCOUNT_COMMAND_ROUTE_HANDLERS
from identity_hub_admin.api.account_detail import ACCOUNT_DETAIL_ROUTE_HANDLERS
from identity_hub_admin.api.accounts import ACCOUNT_ROUTE_HANDLERS
from identity_hub_admin.api.analytics import ANALYTICS_ROUTE_HANDLERS
from identity_hub_admin.api.auth import AUTH_ROUTE_HANDLERS
from identity_hub_admin.api.background import BACKGROUND_ROUTE_HANDLERS
from identity_hub_admin.api.bulk import BULK_ROUTE_HANDLERS
from identity_hub_admin.api.deliveries import DELIVERY_ROUTE_HANDLERS
from identity_hub_admin.api.events import EVENT_ROUTE_HANDLERS
from identity_hub_admin.api.human_queue import HUMAN_QUEUE_ROUTE_HANDLERS
from identity_hub_admin.api.imports import IMPORT_ROUTE_HANDLERS
from identity_hub_admin.api.integration_commands import INTEGRATION_COMMAND_ROUTE_HANDLERS
from identity_hub_admin.api.leases import LEASE_ROUTE_HANDLERS
from identity_hub_admin.api.overview import OVERVIEW_ROUTE_HANDLERS
from identity_hub_admin.api.proxies import PROXY_ROUTE_HANDLERS
from identity_hub_admin.api.resource_commands import RESOURCE_COMMAND_ROUTE_HANDLERS
from identity_hub_admin.api.routes import ROUTE_HANDLERS, route_meta
from identity_hub_admin.api.secret_commands import SECRET_COMMAND_ROUTE_HANDLERS
from identity_hub_admin.api.services import SERVICE_ROUTE_HANDLERS
from identity_hub_admin.auth import AdminAuthenticationMiddleware, AuthService, admin_auth_guard
from identity_hub_admin.bulk import BulkCoordinator
from identity_hub_admin.commands import CommandGateway
from identity_hub_admin.errors import http_exception_handler
from identity_hub_admin.idempotency import IdempotencyCoordinator
from identity_hub_admin.middleware.request_id import RequestIdMiddleware
from identity_hub_admin.middleware.security_headers import SecurityHeadersMiddleware
from identity_hub_admin.observability import AccessLogMiddleware, RequestMetrics
from identity_hub_admin.runtime import AdminRuntime, runtime_lifespan
from identity_hub_admin.settings import AdminSettings
from identity_hub_admin.static_files import spa_handlers


async def _demo_proxy_checker(_url: str) -> bool:
    return True


@get(
    "/health/live",
    operation_id="admin_health_live_get",
    tags=["health"],
    opt=route_meta(auth_required=False),
    sync_to_thread=False,
)
def live() -> dict[str, str]:
    """Return process liveness without touching external dependencies."""
    return {"status": "ok", "version": __version__}


@get(
    "/health/ready",
    operation_id="admin_health_ready_get",
    tags=["health"],
    opt=route_meta(auth_required=False),
    sync_to_thread=False,
)
def ready(state: State) -> Response[dict[str, object]]:
    runtime: AdminRuntime = state.runtime
    status = HTTP_200_OK if runtime.state.ready else HTTP_503_SERVICE_UNAVAILABLE
    return Response(
        {"status": "ready" if runtime.state.ready else "not_ready", "checks": runtime.state.checks},
        status_code=status,
    )


@get(
    "/health/diagnostics",
    operation_id="admin_health_diagnostics_get",
    tags=["health"],
    opt=route_meta(auth_required=False),
    sync_to_thread=False,
)
def diagnostics(request: Request[object, object, State], state: State) -> dict[str, object]:
    """Return non-secret readiness and in-process request metrics."""
    runtime: AdminRuntime = state.runtime
    metrics: RequestMetrics = state.request_metrics
    return {
        "status": "ready" if runtime.state.ready else "not_ready",
        "request_id": str(request.state.request_id),
        "checks": runtime.state.checks,
        "metrics": metrics.snapshot(),
    }


def create_admin_app(
    settings: AdminSettings | None = None,
    *,
    hub: IdentityHub | None = None,
    engine: "AsyncEngine | None" = None,
    resources: tuple[object, ...] = (),
    route_handlers: tuple[object, ...] = (),
    proxy_checker: Any | None = None,
) -> Litestar:
    """Create an isolated admin application instance."""
    settings = settings or AdminSettings()
    if proxy_checker is None and settings.demo_mode:
        proxy_checker = _demo_proxy_checker
    runtime = AdminRuntime(settings=settings, hub=hub, engine=engine, resources=resources)
    auth_service = AuthService(settings)
    idempotency = IdempotencyCoordinator()
    command_gateway = CommandGateway(idempotency)
    bulk_coordinator = BulkCoordinator()
    request_metrics = RequestMetrics()
    handlers: list[Any] = [
        live,
        ready,
        diagnostics,
        *ANALYTICS_ROUTE_HANDLERS,
        *AUTH_ROUTE_HANDLERS,
        *BACKGROUND_ROUTE_HANDLERS,
        *BULK_ROUTE_HANDLERS,
        *DELIVERY_ROUTE_HANDLERS,
        *EVENT_ROUTE_HANDLERS,
        *ACCOUNT_COMMAND_ROUTE_HANDLERS,
        *ACCOUNT_DETAIL_ROUTE_HANDLERS,
        *ACCOUNT_ROUTE_HANDLERS,
        *HUMAN_QUEUE_ROUTE_HANDLERS,
        *INTEGRATION_COMMAND_ROUTE_HANDLERS,
        *IMPORT_ROUTE_HANDLERS,
        *LEASE_ROUTE_HANDLERS,
        *OVERVIEW_ROUTE_HANDLERS,
        *PROXY_ROUTE_HANDLERS,
        *RESOURCE_COMMAND_ROUTE_HANDLERS,
        *SERVICE_ROUTE_HANDLERS,
        *SECRET_COMMAND_ROUTE_HANDLERS,
        *ROUTE_HANDLERS,
        *route_handlers,
    ]
    if settings.demo_mode:
        from identity_hub_admin.api.demo import DEMO_ROUTE_HANDLERS

        handlers.extend(DEMO_ROUTE_HANDLERS)
    if (settings.static_dir / "index.html").is_file() or settings.environment == "production":
        handlers.extend(spa_handlers(settings.static_dir))
    return Litestar(
        route_handlers=handlers,
        middleware=[
            RequestIdMiddleware(),
            DefineMiddleware(AccessLogMiddleware, metrics=request_metrics),
            SecurityHeadersMiddleware(),
            DefineMiddleware(AdminAuthenticationMiddleware, service=auth_service),
        ],
        guards=[admin_auth_guard],
        openapi_config=OpenAPIConfig(
            title="Identity Hub Admin API",
            version="1.0.0",
            description="Versioned administration API over public Identity Hub contracts.",
            path="/docs",
            use_handler_docstrings=True,
        ),
        path=settings.base_path or None,
        state=State(
            {
                "runtime": runtime,
                "auth_service": auth_service,
                "idempotency": idempotency,
                "command_gateway": command_gateway,
                "bulk_coordinator": bulk_coordinator,
                "request_metrics": request_metrics,
                "proxy_checker": proxy_checker,
            }
        ),
        lifespan=[runtime_lifespan],
        exception_handlers={HTTPException: http_exception_handler},
        debug=False,
    )
