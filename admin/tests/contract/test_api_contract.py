from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import uuid4

import pytest
from litestar import Litestar, get
from litestar.params import FromPath
from scripts.export_openapi import OUTPUT, render_openapi

from identity_hub_admin.api.audit import (
    ERROR_STATUS,
    audit_application,
    audit_openapi_document,
    audit_route_metadata,
)
from identity_hub_admin.api.contracts import (
    AdminErrorCode,
    BucketSize,
    CursorPayload,
    MutationHeaders,
    PageQuery,
    TimeRange,
    TimeWindow,
    decode_cursor,
    encode_cursor,
)
from identity_hub_admin.app import create_admin_app

pytestmark = pytest.mark.contract


def test_application_contract_is_complete() -> None:
    assert audit_application(create_admin_app()) == []


def test_openapi_snapshot_is_current() -> None:
    assert OUTPUT.read_text(encoding="utf-8") == render_openapi()


def test_duplicate_operation_id_is_rejected() -> None:
    document = create_admin_app().openapi_schema.to_schema()
    broken = deepcopy(document)
    operations = [
        operation
        for path in broken["paths"].values()
        for method, operation in path.items()
        if method in {"get", "post", "put", "patch", "delete"}
    ]
    operations[1]["operationId"] = operations[0]["operationId"]

    assert any("duplicate operationId" in error for error in audit_openapi_document(broken))


@pytest.mark.parametrize("field", ["password", "token", "secret", "credentials", "evidence"])
def test_secret_field_is_rejected(field: str) -> None:
    broken: dict[str, Any] = {
        "paths": {},
        "components": {"schemas": {"Leaky": {"properties": {field: {}}}}},
    }

    assert any("forbidden secret field" in error for error in audit_openapi_document(broken))


def test_login_password_is_explicitly_write_only() -> None:
    document = create_admin_app().openapi_schema.to_schema()
    login_schema = document["components"]["schemas"]["LoginRequest"]

    assert login_schema["properties"]["password"]["writeOnly"] is True
    assert not any("password" in error for error in audit_openapi_document(document))


def test_route_without_role_and_space_metadata_is_rejected() -> None:
    @get(
        "/api/v1/spaces/{space_slug:str}/broken",
        operation_id="broken",
        sync_to_thread=False,
    )
    def broken(space_slug: FromPath[str]) -> dict[str, str]:
        return {"space_slug": space_slug}

    errors = audit_route_metadata(Litestar(route_handlers=[broken]))

    assert any("missing admin_roles" in error for error in errors)
    assert any("space_scoped" in error for error in errors)


def test_cursor_round_trip_keeps_sort_value_and_uuid_tie_breaker() -> None:
    payload = CursorPayload(sort="created_at", value="2026-09-27T00:00:00Z", id=uuid4())

    assert decode_cursor(encode_cursor(payload)) == payload
    assert PageQuery(limit=200).limit == 200
    with pytest.raises(ValueError, match="invalid cursor payload"):
        decode_cursor("bm90LWEtc3RhYmxlLWN1cnNvcg")


def test_error_registry_has_stable_http_mapping() -> None:
    assert set(ERROR_STATUS) == set(AdminErrorCode)
    assert ERROR_STATUS[AdminErrorCode.REVISION_MISMATCH] == 412
    assert ERROR_STATUS[AdminErrorCode.VALIDATION_FAILED] == 422


def test_mutation_and_time_range_validation() -> None:
    headers = MutationHeaders.model_validate({"Idempotency-Key": "request:12345678"})
    assert headers.idempotency_key == "request:12345678"
    with pytest.raises(ValueError):
        MutationHeaders.model_validate({"Idempotency-Key": "short"})
    with pytest.raises(ValueError):
        TimeRange(
            window=TimeWindow.CUSTOM,
            bucket=BucketSize.HOUR_1,
            timezone="Europe/Simferopol",
        )
