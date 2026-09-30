"""Stable, non-secret HTTP error serialization."""

from __future__ import annotations

from typing import Any

from litestar import Request, Response
from litestar.datastructures import State
from litestar.exceptions import HTTPException

from identity_hub_admin.api.contracts import AdminErrorCode, ErrorBody, ErrorEnvelope
from identity_hub_admin.idempotency import IdempotencyConflict, IdempotencyResultConsumed

_STATUS_CODES: dict[int, AdminErrorCode] = {
    400: AdminErrorCode.VALIDATION_FAILED,
    401: AdminErrorCode.AUTH_REQUIRED,
    403: AdminErrorCode.ACCESS_DENIED,
    404: AdminErrorCode.NOT_FOUND,
    409: AdminErrorCode.CONFLICT,
    412: AdminErrorCode.REVISION_MISMATCH,
    422: AdminErrorCode.VALIDATION_FAILED,
    429: AdminErrorCode.RATE_LIMITED,
    503: AdminErrorCode.DEPENDENCY_UNAVAILABLE,
}
_SAFE_DETAIL_KEYS = frozenset(
    {
        "current_revision",
        "expected_revision",
        "retry_at",
    }
)


def _safe_details(exception: HTTPException) -> dict[str, str | int | float | bool | None]:
    extra = exception.extra
    if not isinstance(extra, dict):
        return {}
    return {
        key: value
        for key, value in extra.items()
        if key in _SAFE_DETAIL_KEYS and isinstance(value, str | int | float | bool | None)
    }


def http_exception_handler(
    request: Request[Any, Any, State], exception: HTTPException
) -> Response[ErrorEnvelope]:
    """Convert framework and guard failures to the documented API envelope."""
    status_code = exception.status_code
    code = (
        AdminErrorCode.IDEMPOTENCY_CONFLICT
        if isinstance(exception, IdempotencyConflict | IdempotencyResultConsumed)
        else _STATUS_CODES.get(status_code, AdminErrorCode.INTERNAL_ERROR)
    )
    detail = exception.detail
    message = str(detail) if status_code < 500 and detail else "Unexpected server error"
    request_id = str(getattr(request.state, "request_id", "unknown"))
    return Response(
        ErrorEnvelope(
            error=ErrorBody(
                code=code,
                message=message,
                details=_safe_details(exception),
                request_id=request_id,
            )
        ),
        status_code=status_code,
        headers={**(exception.headers or {}), "Cache-Control": "no-store"},
    )
