"""Resumable CSV/JSONL imports with bounded, secret-safe parsing."""

from __future__ import annotations

import csv
import io
import json
import time
from datetime import datetime
from pathlib import PurePath
from typing import Any, Literal, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from identity_hub.errors import NotFound
from identity_hub.importing import ImportRecord
from identity_hub.read.admin_state import ImportRunInfo
from litestar import Request, get, post
from litestar.datastructures import State
from litestar.exceptions import HTTPException
from litestar.params import FromPath
from pydantic import Field, ValidationError, field_validator

from identity_hub_admin.api.contracts import ContractModel, MutationContext, ResourceEnvelope
from identity_hub_admin.api.routes import error_responses, route_meta
from identity_hub_admin.auth import AdminPrincipal
from identity_hub_admin.commands import CommandGateway, CommandScope
from identity_hub_admin.context import resolve_space_context

MAX_IMPORT_BYTES = 10 * 1024 * 1024
MAX_IMPORT_RECORDS = 10_000
MAX_PARSE_SECONDS = 5.0
_JSON_FIELDS = frozenset(
    {"attributes", "profile", "labels", "credentials", "sessions", "links", "verifications"}
)
_COLUMNS = frozenset(
    {
        "external_ref",
        "id",
        "attributes",
        "profile",
        "labels",
        "tags",
        "note",
        "credentials",
        "sessions",
        "links",
        "proxy",
        "verifications",
    }
)


class ImportRecordInput(ContractModel):
    external_ref: str = Field(min_length=1, max_length=256)
    id: UUID | None = None
    attributes: dict[str, Any] = Field(default_factory=dict, max_length=100)
    profile: dict[str, Any] = Field(default_factory=dict, max_length=100)
    labels: dict[str, str | int | float | bool] = Field(default_factory=dict)

    @field_validator("labels")
    @classmethod
    def validate_labels(
        cls, value: dict[str, str | int | float | bool]
    ) -> dict[str, str | int | float | bool]:
        if len(value) > 100:
            raise ValueError("labels must contain at most 100 entries")
        return value

    tags: list[str] = Field(default_factory=list, max_length=100)
    note: str | None = Field(default=None, max_length=10_000)
    credentials: dict[str, Any] | None = Field(default=None, max_length=100)
    sessions: dict[str, dict[str, Any]] = Field(default_factory=dict, max_length=20)
    links: dict[str, str] = Field(default_factory=dict, max_length=100)
    proxy: str | None = Field(default=None, max_length=2048)
    verifications: dict[str, str] = Field(default_factory=dict, max_length=100)

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: list[str]) -> list[str]:
        if any(not tag or len(tag) > 64 for tag in value):
            raise ValueError("tags must be non-empty and at most 64 characters")
        return value

    def domain(self) -> ImportRecord:
        return ImportRecord(
            external_ref=self.external_ref,
            id=self.id,
            attributes=self.attributes,
            profile=self.profile,
            labels=self.labels,
            tags=frozenset(self.tags),
            note=self.note,
            credentials=self.credentials,
            sessions=self.sessions,
            links=self.links,
            proxy=self.proxy,
            verifications=self.verifications,
        )


class ImportStartCommand(MutationContext):
    service: str = Field(min_length=1, max_length=64)
    source: str = Field(min_length=1, max_length=256)
    format: Literal["csv", "jsonl"]
    content: str = Field(
        min_length=1,
        max_length=MAX_IMPORT_BYTES,
        json_schema_extra={"writeOnly": True},
    )
    on_existing: Literal["skip", "update_session", "fail"] = "skip"
    verify_sessions: bool = False
    labels: dict[str, str | int | float | bool] = Field(default_factory=dict)

    @field_validator("labels")
    @classmethod
    def validate_labels(
        cls, value: dict[str, str | int | float | bool]
    ) -> dict[str, str | int | float | bool]:
        if len(value) > 100:
            raise ValueError("labels must contain at most 100 entries")
        return value

    @field_validator("source")
    @classmethod
    def safe_source(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        name = PurePath(normalized).name
        if name in {"", ".", ".."} or "\x00" in name:
            raise ValueError("source must be a safe filename")
        return name

    @field_validator("content")
    @classmethod
    def byte_limit(cls, value: str) -> str:
        if len(value.encode("utf-8")) > MAX_IMPORT_BYTES:
            raise ValueError("import exceeds the 10 MiB limit")
        return value


class ImportCancelCommand(MutationContext):
    pass


class ImportReportItem(ContractModel):
    position: int
    external_ref: str | None = None
    code: str | None = None
    reason: str | None = None


class ImportRunResponse(ContractModel):
    id: UUID
    service: str
    actor: str
    source: str
    state: Literal["pending", "running", "succeeded", "failed", "cancelling", "cancelled"]
    on_existing: Literal["skip", "update_session", "fail"]
    verify_sessions: bool
    labels: dict[str, Any]
    started_at: datetime
    updated_at: datetime
    finished_at: datetime | None
    cancel_requested_at: datetime | None
    total: int
    processed: int
    created: int
    updated: int
    skipped: int
    failed: int
    created_ids: list[UUID]
    skipped_items: list[ImportReportItem]
    failed_items: list[ImportReportItem]


class ImportRunsResponse(ContractModel):
    items: list[ImportRunResponse]
    limits: dict[str, int | float]


def _report_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _response(run: ImportRunInfo) -> ImportRunResponse:
    report = run.report
    created_values = _report_list(report.get("created_ids", []))
    skipped_values = _report_list(report.get("skipped", []))
    failed_values = _report_list(report.get("failed", []))
    return ImportRunResponse(
        **{key: getattr(run, key) for key in ImportRunResponse.model_fields if hasattr(run, key)},
        created_ids=[UUID(str(value)) for value in created_values],
        skipped_items=[ImportReportItem.model_validate(value) for value in skipped_values],
        failed_items=[ImportReportItem.model_validate(value) for value in failed_values],
    )


def _gateway(state: State) -> CommandGateway:
    return cast(CommandGateway, state.command_gateway)


def _safe_record(raw: object, line: int) -> ImportRecord:
    try:
        _check_shape(raw)
        return ImportRecordInput.model_validate(raw).domain()
    except (ValidationError, ValueError, TypeError):
        raise HTTPException(
            f"invalid import record at line {line}",
            status_code=422,
            extra={"line": line, "code": "invalid_import_record"},
        ) from None


def _check_shape(value: object, *, depth: int = 0) -> None:
    if depth > 6:
        raise ValueError("record nesting is too deep")
    if isinstance(value, dict):
        if len(value) > 256:
            raise ValueError("record has too many fields")
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 128:
                raise ValueError("invalid field name")
            _check_shape(item, depth=depth + 1)
    elif isinstance(value, list):
        if len(value) > 10_000:
            raise ValueError("array is too large")
        for item in value:
            _check_shape(item, depth=depth + 1)
    elif isinstance(value, str) and len(value) > MAX_IMPORT_BYTES:
        raise ValueError("field is too large")


def _parse_jsonl(content: str, started: float) -> list[ImportRecord]:
    records: list[ImportRecord] = []
    for line_number, line in enumerate(content.splitlines(), start=1):
        if not line.strip():
            continue
        if len(records) >= MAX_IMPORT_RECORDS:
            raise HTTPException("import exceeds the 10000 record limit", status_code=413)
        if time.monotonic() - started > MAX_PARSE_SECONDS:
            raise HTTPException("import parsing exceeded the time limit", status_code=408)
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            raise HTTPException(
                f"invalid JSON at line {line_number}",
                status_code=422,
                extra={"line": line_number, "code": "invalid_json"},
            ) from None
        records.append(_safe_record(raw, line_number))
    return records


def _parse_csv(content: str, started: float) -> list[ImportRecord]:
    reader = csv.DictReader(io.StringIO(content, newline=""))
    fields = set(reader.fieldnames or ())
    unknown = fields - _COLUMNS
    if "external_ref" not in fields or unknown:
        detail = "CSV requires external_ref" if "external_ref" not in fields else "CSV has unknown columns"
        raise HTTPException(detail, status_code=422, extra={"columns": sorted(unknown)})
    records: list[ImportRecord] = []
    for line_number, row in enumerate(reader, start=2):
        if len(records) >= MAX_IMPORT_RECORDS:
            raise HTTPException("import exceeds the 10000 record limit", status_code=413)
        if time.monotonic() - started > MAX_PARSE_SECONDS:
            raise HTTPException("import parsing exceeded the time limit", status_code=408)
        raw: dict[str, Any] = {key: value for key, value in row.items() if value not in {None, ""}}
        try:
            for field in _JSON_FIELDS & raw.keys():
                raw[field] = json.loads(raw[field])
            if "tags" in raw:
                value = raw["tags"]
                raw["tags"] = json.loads(value) if value.lstrip().startswith("[") else value.split(";")
        except (json.JSONDecodeError, TypeError):
            raise HTTPException(
                f"invalid structured CSV field at line {line_number}",
                status_code=422,
                extra={"line": line_number, "code": "invalid_csv_field"},
            ) from None
        records.append(_safe_record(raw, line_number))
    return records


def parse_import(data: ImportStartCommand) -> list[ImportRecord]:
    started = time.monotonic()
    records = (
        _parse_jsonl(data.content, started) if data.format == "jsonl" else _parse_csv(data.content, started)
    )
    if not records:
        raise HTTPException("import contains no records", status_code=422)
    refs = [record.external_ref for record in records]
    if len(refs) != len(set(refs)):
        raise HTTPException("import contains duplicate external_ref values", status_code=422)
    return records


@get(
    "/api/v1/spaces/{space_slug:str}/imports",
    operation_id="admin_v1_imports_get",
    tags=["imports"],
    opt=route_meta("viewer", "operator", "admin", space_scoped=True),
    responses=error_responses(401, 403, 404),
)
async def imports_list(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
) -> ResourceEnvelope[ImportRunsResponse]:
    resolved = await resolve_space_context(request, state, space_slug, require_idempotency=False)
    runs = await resolved.handle.admin.import_runs(limit=100)
    return ResourceEnvelope(
        data=ImportRunsResponse(
            items=[_response(run) for run in runs],
            limits={
                "max_bytes": MAX_IMPORT_BYTES,
                "max_records": MAX_IMPORT_RECORDS,
                "max_parse_seconds": MAX_PARSE_SECONDS,
            },
        )
    )


@get(
    "/api/v1/spaces/{space_slug:str}/imports/{run_id:uuid}",
    operation_id="admin_v1_import_get",
    tags=["imports"],
    opt=route_meta("viewer", "operator", "admin", space_scoped=True),
    responses=error_responses(401, 403, 404),
)
async def import_detail(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
    run_id: FromPath[UUID],
) -> ResourceEnvelope[ImportRunResponse]:
    resolved = await resolve_space_context(request, state, space_slug, require_idempotency=False)
    try:
        run = await resolved.handle.imports.get(run_id)
    except NotFound:
        raise HTTPException("import run not found", status_code=404) from None
    return ResourceEnvelope(data=_response(run))


@post(
    "/api/v1/spaces/{space_slug:str}/imports",
    operation_id="admin_v1_import_start_post",
    tags=["imports"],
    opt=route_meta("operator", "admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 408, 409, 413, 422),
)
async def import_start(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
    data: ImportStartCommand,
) -> ResourceEnvelope[ImportRunResponse]:
    records = parse_import(data)
    key = request.headers.get("Idempotency-Key", "")
    run_id = uuid5(NAMESPACE_URL, f"identity-hub:admin-import:{space_slug}:{key}")

    async def apply(scope: CommandScope) -> ResourceEnvelope[ImportRunResponse]:
        await scope.handle.imports.stage(
            service=data.service,
            records=records,
            source=data.source,
            on_existing=data.on_existing,
            verify_sessions=data.verify_sessions,
            labels=data.labels,
            actor=scope.actor,
            run_id=run_id,
        )
        await scope.handle.admin.record_import(
            "started",
            run_id,
            actor=scope.actor,
            reason=scope.reason,
            service=data.service,
            count=len(records),
        )
        run = await scope.handle.imports.get(run_id)
        return ResourceEnvelope(data=_response(run))

    return await _gateway(state).execute(
        request=request,
        state=state,
        space_slug=space_slug,
        operation="admin.import.start",
        mutation=data,
        call=apply,
        payload={
            "service": data.service,
            "source": data.source,
            "format": data.format,
            "record_count": len(records),
        },
        sensitive=True,
    )


@post(
    "/api/v1/spaces/{space_slug:str}/imports/{run_id:uuid}/cancel",
    operation_id="admin_v1_import_cancel_post",
    tags=["imports"],
    opt=route_meta("operator", "admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 409, 422),
)
async def import_cancel(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
    run_id: FromPath[UUID],
    data: ImportCancelCommand,
) -> ResourceEnvelope[ImportRunResponse]:
    async def apply(scope: CommandScope) -> ResourceEnvelope[ImportRunResponse]:
        try:
            before = await scope.handle.imports.get(run_id)
        except NotFound:
            raise HTTPException("import run not found", status_code=404) from None
        await scope.handle.imports.cancel(run_id)
        await scope.handle.admin.record_import(
            "cancel_requested",
            run_id,
            actor=scope.actor,
            reason=scope.reason,
            service=before.service,
        )
        after = await scope.handle.imports.get(run_id)
        return ResourceEnvelope(data=_response(after))

    return await _gateway(state).execute(
        request=request,
        state=state,
        space_slug=space_slug,
        operation="admin.import.cancel",
        mutation=data,
        call=apply,
        payload={"run_id": str(run_id)},
    )


IMPORT_ROUTE_HANDLERS = [imports_list, import_detail, import_start, import_cancel]
