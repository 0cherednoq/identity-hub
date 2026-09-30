"""Contract checks used by tests and OpenAPI export."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from litestar import Litestar
from litestar.routes import HTTPRoute

from identity_hub_admin.api.contracts import AdminErrorCode

ALLOWED_ROLES = {"viewer", "operator", "admin"}
FORBIDDEN_SCHEMA_FIELDS = {
    "bot_token",
    "ciphertext",
    "credentials",
    "evidence",
    "password",
    "secret",
    "session_payload",
    "token",
    "wrapped_dek",
}


def _operations(document: Mapping[str, Any]) -> list[tuple[str, str, Mapping[str, Any]]]:
    result: list[tuple[str, str, Mapping[str, Any]]] = []
    for path, path_item in document.get("paths", {}).items():
        if not isinstance(path_item, Mapping):
            continue
        for method, operation in path_item.items():
            if method in {"get", "post", "put", "patch", "delete"} and isinstance(operation, Mapping):
                result.append((str(path), str(method), operation))
    return result


def audit_openapi_document(document: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    for path, method, operation in _operations(document):
        operation_id = operation.get("operationId")
        if not isinstance(operation_id, str) or not operation_id:
            errors.append(f"{method.upper()} {path}: operationId is required")
        elif operation_id in seen:
            errors.append(f"duplicate operationId: {operation_id}")
        else:
            seen.add(operation_id)

    def visit(value: Any, location: str) -> None:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                normalized = str(key).casefold()
                explicitly_write_only = isinstance(nested, Mapping) and nested.get("writeOnly") is True
                if normalized in FORBIDDEN_SCHEMA_FIELDS and not explicitly_write_only:
                    errors.append(f"forbidden secret field at {location}.{key}")
                visit(nested, f"{location}.{key}")
        elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
            for index, nested in enumerate(value):
                visit(nested, f"{location}[{index}]")

    visit(document, "openapi")
    return errors


def audit_route_metadata(app: Litestar) -> list[str]:
    errors: list[str] = []
    docs_path = (app.openapi_config.path if app.openapi_config else None) or "/docs"
    for route in app.routes:
        if not isinstance(route, HTTPRoute):
            continue
        if route.path.startswith(docs_path):
            continue
        for handler in route.route_handlers:
            methods = set(getattr(handler, "http_methods", ()))
            if not methods or methods <= {"HEAD", "OPTIONS"}:
                continue
            label = f"{','.join(sorted(methods))} {route.path}"
            if not handler.operation_id:
                errors.append(f"{label}: operation_id is required")
            opt = handler.opt
            for key in ("admin_roles", "auth_required", "space_scoped"):
                if key not in opt:
                    errors.append(f"{label}: missing {key} metadata")
            roles = opt.get("admin_roles")
            if not isinstance(roles, tuple) or not set(roles) <= ALLOWED_ROLES:
                errors.append(f"{label}: invalid admin_roles metadata")
            if not isinstance(opt.get("auth_required"), bool):
                errors.append(f"{label}: invalid auth_required metadata")
            if opt.get("auth_required") is True and not roles:
                errors.append(f"{label}: authenticated route requires at least one role")
            expected_scope = "{space_slug" in route.path
            if opt.get("space_scoped") is not expected_scope:
                errors.append(f"{label}: space_scoped does not match path")
    return errors


def audit_application(app: Litestar) -> list[str]:
    document = app.openapi_schema.to_schema()
    return [*audit_openapi_document(document), *audit_route_metadata(app)]


ERROR_STATUS: dict[AdminErrorCode, int] = {
    AdminErrorCode.AUTH_REQUIRED: 401,
    AdminErrorCode.ACCESS_DENIED: 403,
    AdminErrorCode.NOT_FOUND: 404,
    AdminErrorCode.CONFLICT: 409,
    AdminErrorCode.IDEMPOTENCY_CONFLICT: 409,
    AdminErrorCode.REVISION_MISMATCH: 412,
    AdminErrorCode.VALIDATION_FAILED: 422,
    AdminErrorCode.RATE_LIMITED: 429,
    AdminErrorCode.DEPENDENCY_UNAVAILABLE: 503,
    AdminErrorCode.INTERNAL_ERROR: 500,
}
