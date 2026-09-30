"""Secret-free account list and search endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from identity_hub.errors import ValidationFailed
from identity_hub.read.search import AccountListItem, AccountSearchOptions
from litestar import Request, get
from litestar.exceptions import HTTPException, NotFoundException
from litestar.params import FromPath, FromQuery, Parameter

from identity_hub_admin.api.contracts import ContractModel, ResourceEnvelope
from identity_hub_admin.api.routes import error_responses, route_meta


class AccountLeaseResponse(ContractModel):
    pool: str
    holder: str
    acquired_at: datetime
    expires_at: datetime


class AccountProxyResponse(ContractModel):
    id: str
    host: str
    port: int
    status: str


class AccountQuotaResponse(ContractModel):
    kind: str
    window_seconds: int
    used: int
    limit: int
    reset_at: datetime


class AccountListItemResponse(ContractModel):
    id: str
    short_id: str
    service: str
    external_ref: str
    lifecycle: str
    standing: str
    auth: str
    availability: str
    eligible: bool
    blockers: list[str]
    lease: AccountLeaseResponse | None
    proxy: AccountProxyResponse | None
    verification_tier: str | None
    requirements: list[str]
    tags: list[str]
    labels: dict[str, Any]
    profile: dict[str, Any]
    has_override: bool
    last_leased_at: datetime | None
    last_synced_at: datetime | None
    error_score: float
    outgoing_links: int
    incoming_links: int
    quotas: list[AccountQuotaResponse]
    created_at: datetime
    updated_at: datetime


class AccountPageResponse(ContractModel):
    items: list[AccountListItemResponse]
    next_cursor: str | None
    total: int | None
    total_label: str | None


def _item_response(item: AccountListItem) -> AccountListItemResponse:
    return AccountListItemResponse(
        id=str(item.id),
        short_id=item.id.hex[-8:],
        service=item.service,
        external_ref=item.external_ref,
        lifecycle=item.lifecycle,
        standing=item.standing,
        auth=item.auth,
        availability=item.availability,
        eligible=item.eligible,
        blockers=list(item.blockers),
        lease=(
            AccountLeaseResponse(
                pool=item.lease.pool,
                holder=item.lease.holder,
                acquired_at=item.lease.acquired_at,
                expires_at=item.lease.expires_at,
            )
            if item.lease is not None
            else None
        ),
        proxy=(
            AccountProxyResponse(
                id=str(item.proxy.id),
                host=item.proxy.host,
                port=item.proxy.port,
                status=item.proxy.status,
            )
            if item.proxy is not None
            else None
        ),
        verification_tier=item.verification_tier,
        requirements=list(item.requirements),
        tags=list(item.tags),
        labels=item.labels,
        profile=item.profile,
        has_override=item.has_override,
        last_leased_at=item.last_leased_at,
        last_synced_at=item.last_synced_at,
        error_score=item.error_score,
        outgoing_links=item.outgoing_links,
        incoming_links=item.incoming_links,
        quotas=[
            AccountQuotaResponse(
                kind=quota.kind,
                window_seconds=quota.window_seconds,
                used=quota.used,
                limit=quota.limit,
                reset_at=quota.reset_at,
            )
            for quota in item.quotas
        ],
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _query(
    q: str,
    service: str | None,
    lifecycle: str | None,
    standing: str | None,
    auth: str | None,
    availability: str | None,
    tag: str | None,
    label: str | None,
) -> str:
    tokens = [q]
    tokens.extend(
        f"{key}:{value}"
        for key, value in (
            ("service", service),
            ("lifecycle", lifecycle),
            ("standing", standing),
            ("auth", auth),
            ("availability", availability),
            ("tag", tag),
        )
        if value is not None
    )
    if label is not None:
        tokens.append(label)
    return " ".join(token for token in tokens if token)


@get(
    "/api/v1/spaces/{space_slug:str}/accounts",
    operation_id="admin_v1_accounts_get",
    tags=["accounts"],
    opt=route_meta("viewer", "operator", "admin", space_scoped=True),
    responses=error_responses(400, 401, 403, 404, 503),
)
async def accounts(
    request: Request[Any, Any, Any],
    space_slug: FromPath[str],
    q: str = Parameter(default="", max_length=500),
    cursor: str | None = Parameter(default=None, max_length=2048),
    limit: int = Parameter(default=50, ge=1, le=100),
    service: str | None = Parameter(default=None, max_length=64),
    lifecycle: str | None = Parameter(default=None, max_length=32),
    standing: str | None = Parameter(default=None, max_length=32),
    auth: str | None = Parameter(default=None, max_length=32),
    availability: str | None = Parameter(default=None, max_length=32),
    tag: str | None = Parameter(default=None, max_length=64),
    label: str | None = Parameter(default=None, max_length=128),
    attention: bool | None = Parameter(default=None),
    eligible: bool | None = Parameter(default=None),
    leased: bool | None = Parameter(default=None),
    has_override: bool | None = Parameter(default=None),
    requirement: str | None = Parameter(default=None, max_length=128),
    factor: str | None = Parameter(default=None, max_length=128),
    factor_state: str | None = Parameter(default=None, max_length=32),
    tier: str | None = Parameter(default=None, max_length=128),
    proxy: str | None = Parameter(default=None, max_length=255),
    linked: bool | None = Parameter(default=None),
    quota_kind: str | None = Parameter(default=None, max_length=128),
    created_from: FromQuery[datetime | None] = None,
    created_to: FromQuery[datetime | None] = None,
    last_leased_from: FromQuery[datetime | None] = None,
    last_leased_to: FromQuery[datetime | None] = None,
    synced_from: FromQuery[datetime | None] = None,
    synced_to: FromQuery[datetime | None] = None,
    standing_changed_from: FromQuery[datetime | None] = None,
    standing_changed_to: FromQuery[datetime | None] = None,
    sort: str = Parameter(default="created_at", max_length=128),
    direction: FromQuery[Literal["asc", "desc"]] = "desc",
) -> ResourceEnvelope[AccountPageResponse]:
    handle = getattr(request.state, "space_handle", None)
    if handle is None or handle.slug != space_slug:
        raise NotFoundException("space not found")
    options = AccountSearchOptions(
        attention=attention,
        eligible=eligible,
        leased=leased,
        has_override=has_override,
        requirement=requirement,
        factor=factor,
        factor_state=factor_state,
        tier=tier,
        proxy=proxy,
        linked=linked,
        quota_kind=quota_kind,
        created_from=created_from,
        created_to=created_to,
        last_leased_from=last_leased_from,
        last_leased_to=last_leased_to,
        synced_from=synced_from,
        synced_to=synced_to,
        standing_changed_from=standing_changed_from,
        standing_changed_to=standing_changed_to,
        sort_by=sort,
        sort_desc=direction == "desc",
    )
    try:
        page = await handle.account_search(
            _query(q, service, lifecycle, standing, auth, availability, tag, label),
            options=options,
            limit=limit,
            after=cursor,
        )
    except ValidationFailed as exc:
        raise HTTPException(str(exc), status_code=400) from None
    return ResourceEnvelope(
        data=AccountPageResponse(
            items=[_item_response(item) for item in page.items],
            next_cursor=page.next_cursor,
            total=page.total,
            total_label=page.total_label,
        )
    )


ACCOUNT_ROUTE_HANDLERS = [accounts]
