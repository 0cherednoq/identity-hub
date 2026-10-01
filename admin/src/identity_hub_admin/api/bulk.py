"""Bounded server-side bulk actions and secret-free exports."""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from typing import Literal, cast
from uuid import UUID

from identity_hub.errors import IdentityHubError
from identity_hub.read.events import EventFilters
from identity_hub.read.search import AccountSearchOptions
from litestar import Request, Response, post
from litestar.datastructures import State
from litestar.params import FromPath
from pydantic import Field, model_validator

from identity_hub_admin.api.accounts import _query
from identity_hub_admin.api.contracts import (
    Confirmation,
    ContractModel,
    MutationContext,
    ResourceEnvelope,
)
from identity_hub_admin.api.routes import error_responses, route_meta
from identity_hub_admin.auth import AdminPrincipal
from identity_hub_admin.bulk import BulkCoordinator, BulkSnapshot
from identity_hub_admin.commands import CommandGateway, CommandScope
from identity_hub_admin.idempotency import request_fingerprint


class AccountBulkFilter(ContractModel):
    q: str = Field(default="", max_length=500)
    service: str | None = Field(default=None, max_length=64)
    lifecycle: str | None = Field(default=None, max_length=32)
    standing: str | None = Field(default=None, max_length=32)
    auth: str | None = Field(default=None, max_length=32)
    availability: str | None = Field(default=None, max_length=32)
    tag: str | None = Field(default=None, max_length=64)
    label: str | None = Field(default=None, max_length=128)
    attention: bool | None = None
    eligible: bool | None = None
    leased: bool | None = None
    has_override: bool | None = None
    requirement: str | None = Field(default=None, max_length=128)
    factor: str | None = Field(default=None, max_length=128)
    factor_state: str | None = Field(default=None, max_length=32)
    tier: str | None = Field(default=None, max_length=128)
    proxy: str | None = Field(default=None, max_length=255)
    linked: bool | None = None
    quota_kind: str | None = Field(default=None, max_length=128)
    created_from: datetime | None = None
    created_to: datetime | None = None
    last_leased_from: datetime | None = None
    last_leased_to: datetime | None = None
    synced_from: datetime | None = None
    synced_to: datetime | None = None
    standing_changed_from: datetime | None = None
    standing_changed_to: datetime | None = None
    sort: str = Field(default="created_at", max_length=128)
    direction: Literal["asc", "desc"] = "desc"

    def query(self) -> str:
        return _query(
            self.q,
            self.service,
            self.lifecycle,
            self.standing,
            self.auth,
            self.availability,
            self.tag,
            self.label,
        )

    def options(self) -> AccountSearchOptions:
        return AccountSearchOptions(
            attention=self.attention,
            eligible=self.eligible,
            leased=self.leased,
            has_override=self.has_override,
            requirement=self.requirement,
            factor=self.factor,
            factor_state=self.factor_state,
            tier=self.tier,
            proxy=self.proxy,
            linked=self.linked,
            quota_kind=self.quota_kind,
            created_from=self.created_from,
            created_to=self.created_to,
            last_leased_from=self.last_leased_from,
            last_leased_to=self.last_leased_to,
            synced_from=self.synced_from,
            synced_to=self.synced_to,
            standing_changed_from=self.standing_changed_from,
            standing_changed_to=self.standing_changed_to,
            sort_by=self.sort,
            sort_desc=self.direction == "desc",
        )


class BulkOperation(ContractModel):
    kind: Literal["lifecycle", "resync", "tag_add", "tag_remove"]
    target: Literal["provisioning", "ready", "retired"] | None = None
    regions: list[str] = Field(default_factory=list, max_length=20)
    tags: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def validate_parameters(self) -> BulkOperation:
        if self.kind == "lifecycle" and self.target is None:
            raise ValueError("lifecycle bulk action requires target")
        if self.kind == "resync" and not self.regions:
            raise ValueError("resync bulk action requires regions")
        if self.kind in {"tag_add", "tag_remove"} and not self.tags:
            raise ValueError("tag bulk action requires tags")
        return self


class BulkPreviewCommand(MutationContext):
    filter: AccountBulkFilter = Field(default_factory=AccountBulkFilter)
    operation: BulkOperation
    limit: int = Field(default=1000, ge=1, le=1000)


class BulkExecuteCommand(MutationContext):
    snapshot_id: UUID
    confirmation: Confirmation

    @model_validator(mode="after")
    def validate_confirmation(self) -> BulkExecuteCommand:
        if "changes_many_accounts" not in self.confirmation.consequence_codes:
            raise ValueError("bulk execution requires changes_many_accounts acknowledgement")
        return self


class BulkSample(ContractModel):
    identity_id: UUID
    service: str
    external_ref: str
    revision: int


class BulkPreviewResponse(ContractModel):
    snapshot_id: UUID
    operation: str
    selected_count: int
    truncated: bool
    expires_at: datetime
    sample: list[BulkSample]


class BulkFailure(ContractModel):
    identity_id: UUID
    code: str
    message: str


class BulkReportResponse(ContractModel):
    snapshot_id: UUID
    operation: str
    total: int
    succeeded: int
    failed: int
    skipped_already_succeeded: int
    attempt: int
    complete: bool
    failures: list[BulkFailure]


class EventExportFilter(ContractModel):
    type: str | None = None
    group: str | None = Field(default=None, max_length=64)
    severity: Literal["info", "warning", "critical"] | None = None
    service: str | None = Field(default=None, max_length=64)
    identity_id: UUID | None = None
    proxy_id: UUID | None = None
    lease_id: UUID | None = None
    actor: str | None = Field(default=None, max_length=256)
    occurred_from: datetime | None = None
    occurred_to: datetime | None = None
    include_journal: bool = False

    def domain(self) -> EventFilters:
        return EventFilters(**self.model_dump())


class ExportCommand(MutationContext):
    source: Literal["accounts", "events"]
    format: Literal["csv", "json"]
    account_filter: AccountBulkFilter | None = None
    event_filter: EventExportFilter | None = None
    limit: int = Field(default=1000, ge=1, le=1000)


def _gateway(state: State) -> CommandGateway:
    return cast(CommandGateway, state.command_gateway)


def _coordinator(state: State) -> BulkCoordinator:
    return cast(BulkCoordinator, state.bulk_coordinator)


def _preview(snapshot: BulkSnapshot) -> BulkPreviewResponse:
    return BulkPreviewResponse(
        snapshot_id=snapshot.id,
        operation=snapshot.operation,
        selected_count=len(snapshot.entries),
        truncated=snapshot.truncated,
        expires_at=snapshot.expires_at,
        sample=[
            BulkSample(
                identity_id=item.id,
                service=item.service,
                external_ref=item.external_ref,
                revision=item.revision,
            )
            for item in snapshot.entries[:10]
        ],
    )


@post(
    "/api/v1/spaces/{space_slug:str}/bulk/preview",
    operation_id="admin_v1_bulk_preview_post",
    tags=["bulk"],
    opt=route_meta("operator", "admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 409, 422),
)
async def bulk_preview(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
    data: BulkPreviewCommand,
) -> ResourceEnvelope[BulkPreviewResponse]:
    async def apply(scope: CommandScope) -> ResourceEnvelope[BulkPreviewResponse]:
        selected = await scope.handle.admin.selection_snapshot(
            data.filter.query(), options=data.filter.options(), limit=data.limit
        )
        snapshot = await _coordinator(state).create(
            space_slug=space_slug,
            actor=scope.actor,
            operation=data.operation.kind,
            parameters=data.operation.model_dump(mode="json"),
            filter_fingerprint=request_fingerprint(data.filter),
            entries=selected.entries,
            truncated=selected.truncated,
        )
        return ResourceEnvelope(data=_preview(snapshot))

    return await _gateway(state).execute(
        request=request,
        state=state,
        space_slug=space_slug,
        operation="admin.bulk.preview",
        mutation=data,
        call=apply,
        payload={"operation": data.operation.kind},
        transaction_mode="external",
    )


async def _run_one(scope: CommandScope, snapshot: BulkSnapshot, identity_id: UUID, revision: int) -> None:
    card = await scope.handle.identities.card(identity_id)
    if card.revision != revision:
        raise ValueError("account revision changed after preview")
    parameters = snapshot.parameters
    if snapshot.operation == "lifecycle":
        await scope.handle.identities.set_lifecycle(
            identity_id,
            cast(str, parameters["target"]),
            actor=scope.actor,
            reason=scope.reason,
        )
    elif snapshot.operation == "resync":
        await scope.handle.sync.refresh(identity_id, cast(list[str], parameters["regions"]))
    elif snapshot.operation == "tag_add":
        await scope.handle.identities.add_tags(
            identity_id, cast(list[str], parameters["tags"]), actor=scope.actor
        )
    else:
        await scope.handle.identities.remove_tags(
            identity_id, cast(list[str], parameters["tags"]), actor=scope.actor
        )


def _failure(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, ValueError):
        return "revision_mismatch", "account changed after preview"
    if isinstance(exc, IdentityHubError):
        return type(exc).__name__.lower(), "domain command rejected"
    return "dependency_failure", "command dependency failed"


@post(
    "/api/v1/spaces/{space_slug:str}/bulk/execute",
    operation_id="admin_v1_bulk_execute_post",
    tags=["bulk"],
    opt=route_meta("operator", "admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 409, 422),
)
async def bulk_execute(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
    data: BulkExecuteCommand,
) -> ResourceEnvelope[BulkReportResponse]:
    async def apply(scope: CommandScope) -> ResourceEnvelope[BulkReportResponse]:
        snapshot = await _coordinator(state).get(data.snapshot_id, space_slug=space_slug, actor=scope.actor)
        async with snapshot.execution_lock:
            already = len(snapshot.succeeded)
            succeeded: set[UUID] = set()
            failures: dict[UUID, tuple[str, str]] = {}
            for entry in snapshot.entries:
                if entry.id in snapshot.succeeded:
                    continue
                try:
                    await _run_one(scope, snapshot, entry.id, entry.revision)
                except Exception as exc:  # one bad account must not abort the bounded batch
                    failures[entry.id] = _failure(exc)
                else:
                    succeeded.add(entry.id)
            await _coordinator(state).update(snapshot, succeeded=succeeded, failures=failures)
            await scope.handle.admin.record_bulk(
                snapshot.operation,
                actor=scope.actor,
                reason=scope.reason,
                snapshot_id=snapshot.id,
                succeeded=len(succeeded),
                failed=len(failures),
                attempt=snapshot.attempts,
            )
            return ResourceEnvelope(
                data=BulkReportResponse(
                    snapshot_id=snapshot.id,
                    operation=snapshot.operation,
                    total=len(snapshot.entries),
                    succeeded=len(snapshot.succeeded),
                    failed=len(failures),
                    skipped_already_succeeded=already,
                    attempt=snapshot.attempts,
                    complete=len(snapshot.succeeded) == len(snapshot.entries),
                    failures=[
                        BulkFailure(identity_id=identity_id, code=value[0], message=value[1])
                        for identity_id, value in failures.items()
                    ],
                )
            )

    return await _gateway(state).execute(
        request=request,
        state=state,
        space_slug=space_slug,
        operation="admin.bulk.execute",
        mutation=data,
        call=apply,
        payload={"snapshot_id": str(data.snapshot_id)},
        transaction_mode="external",
    )


def _safe_cell(value: object) -> str:
    if value is None:
        rendered = ""
    elif isinstance(value, str):
        rendered = value
    elif isinstance(value, datetime):
        rendered = value.isoformat()
    elif isinstance(value, list | tuple | dict):
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    else:
        rendered = str(value)
    dangerous = rendered.startswith(("\t", "\r", "\n")) or rendered.lstrip().startswith(("=", "+", "-", "@"))
    return f"'{rendered}" if dangerous else rendered


def _render_export(rows: list[dict[str, object]], format: Literal["csv", "json"]) -> tuple[bytes, str]:
    if format == "json":
        return (
            json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str).encode(),
            "application/json",
        )
    output = io.StringIO(newline="")
    columns = list(rows[0]) if rows else ["id"]
    writer = csv.DictWriter(output, fieldnames=columns, lineterminator="\r\n")
    writer.writeheader()
    writer.writerows({key: _safe_cell(value) for key, value in row.items()} for row in rows)
    return output.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8"


async def _account_export(scope: CommandScope, data: ExportCommand) -> tuple[list[dict[str, object]], bool]:
    filters = data.account_filter or AccountBulkFilter()
    selected = await scope.handle.admin.selection_snapshot(
        filters.query(), options=filters.options(), limit=data.limit
    )
    rows: list[dict[str, object]] = []
    for entry in selected.entries:
        card = await scope.handle.identities.card(entry.id)
        identity = card.identity
        rows.append(
            {
                "id": str(identity.id),
                "service": identity.service,
                "external_ref": identity.external_ref,
                "lifecycle": identity.lifecycle,
                "standing": identity.standing,
                "auth": identity.auth_state,
                "availability": identity.availability,
                "eligible": card.eligible,
                "tags": identity.tags,
                "labels": identity.labels,
                "profile": identity.profile,
                "created_at": identity.created_at,
                "updated_at": identity.updated_at,
                "revision": card.revision,
            }
        )
    return rows, selected.truncated


async def _event_export(scope: CommandScope, data: ExportCommand) -> tuple[list[dict[str, object]], bool]:
    filters = (data.event_filter or EventExportFilter()).domain()
    rows: list[dict[str, object]] = []
    before: tuple[datetime, UUID] | None = None
    while len(rows) < data.limit:
        page = await scope.handle.admin.event_page(
            filters=filters,
            limit=min(200, data.limit - len(rows)),
            before=before,
        )
        rows.extend(
            {
                "event_id": str(item.event_id),
                "type": item.type,
                "group": item.group,
                "severity": item.severity,
                "service": item.service,
                "identity_id": str(item.identity_id) if item.identity_id else None,
                "proxy_id": str(item.proxy_id) if item.proxy_id else None,
                "lease_id": str(item.lease_id) if item.lease_id else None,
                "sequence": item.sequence,
                "actor": item.actor,
                "summary": item.summary,
                "data": dict(item.data),
                "occurred_at": item.occurred_at,
            }
            for item in page.items
        )
        before = page.next_before
        if before is None:
            return rows, False
    return rows, before is not None


@post(
    "/api/v1/spaces/{space_slug:str}/exports",
    operation_id="admin_v1_export_post",
    tags=["exports"],
    opt=route_meta("operator", "admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 409, 422),
)
async def export_data(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
    data: ExportCommand,
) -> Response[bytes]:
    async def apply(scope: CommandScope) -> Response[bytes]:
        rows, truncated = (
            await _account_export(scope, data)
            if data.source == "accounts"
            else await _event_export(scope, data)
        )
        payload, media_type = _render_export(rows, data.format)
        filters: ContractModel = (
            data.account_filter or AccountBulkFilter()
            if data.source == "accounts"
            else data.event_filter or EventExportFilter()
        )
        await scope.handle.admin.record_export(
            data.source,
            actor=scope.actor,
            reason=scope.reason,
            format=data.format,
            count=len(rows),
            filter_fingerprint=request_fingerprint(filters),
        )
        return Response(
            payload,
            media_type=media_type,
            headers={
                "Cache-Control": "no-store, private, max-age=0",
                "Content-Disposition": f'attachment; filename="{data.source}.{data.format}"',
                "X-Export-Rows": str(len(rows)),
                "X-Export-Truncated": str(truncated).lower(),
            },
        )

    return await _gateway(state).execute(
        request=request,
        state=state,
        space_slug=space_slug,
        operation=f"admin.{data.source}.export",
        mutation=data,
        call=apply,
        payload={"source": data.source, "format": data.format},
        transaction_mode="external",
    )


BULK_ROUTE_HANDLERS = [bulk_preview, bulk_execute, export_data]
