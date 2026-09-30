"""Read-only, secret-free account card endpoints."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID

from identity_hub.errors import NotFound
from identity_hub.hub import SpaceHandle
from identity_hub.read.cards import IdentityCard
from litestar import Request, get
from litestar.exceptions import NotFoundException
from litestar.params import FromPath
from pydantic import Field

from identity_hub_admin.api.contracts import ContractModel, ResourceEnvelope
from identity_hub_admin.api.routes import error_responses, route_meta
from identity_hub_admin.auth import AdminPrincipal, AdminRole

AccountSection = Literal[
    "state",
    "profile",
    "access",
    "verification",
    "leases",
    "network",
    "events",
    "logs",
]


class AccountHeaderResponse(ContractModel):
    id: str
    short_id: str
    space: str
    service: str
    external_ref: str
    lifecycle: str
    standing: str
    auth: str
    availability: str
    eligible: bool
    blockers: list[str]
    revision: int
    created_at: datetime
    updated_at: datetime
    lease: dict[str, Any] | None


class AccountActionResponse(ContractModel):
    id: str
    kind: Literal[
        "lifecycle",
        "restore",
        "override",
        "unpin",
        "resolve_requirement",
        "run_login",
        "resync",
        "answer_challenge",
        "verification_approve",
        "verification_reject",
        "verification_abandon",
        "lease_revoke",
        "proxy_rebind",
        "link",
        "unlink",
    ]
    label: str
    required_role: Literal["operator", "admin"]
    region: str | None = None
    key: str = ""
    target: str | None = None
    allowed_targets: list[str] = Field(default_factory=list)
    requires_confirmation: bool = False
    consequence_codes: list[str] = Field(default_factory=list)
    pin_supported: bool = False
    requires_role_input: bool = False


class SecretDescriptorResponse(ContractModel):
    field: str
    label: str
    update_kind: Literal["credentials", "session"] | None = None


class AccountSectionResponse(ContractModel):
    section: AccountSection
    revision: int
    available: bool = True
    reason: str | None = None
    data: dict[str, Any]
    actions: list[AccountActionResponse] = Field(default_factory=list)
    secrets: list[SecretDescriptorResponse] = Field(default_factory=list)


def _handle(request: Request[Any, Any, Any], space_slug: str) -> SpaceHandle:
    handle = getattr(request.state, "space_handle", None)
    if handle is None or handle.slug != space_slug:
        raise NotFoundException("space not found")
    return cast(SpaceHandle, handle)


async def _card(request: Request[Any, Any, Any], space_slug: str, identity_id: UUID) -> IdentityCard:
    try:
        return await _handle(request, space_slug).identity_card(identity_id)
    except NotFound:
        raise NotFoundException("account not found") from None


def _lease(card: IdentityCard) -> dict[str, Any] | None:
    if card.lease is None:
        return None
    return {
        "lease_id": str(card.lease.lease_id),
        "pool": card.lease.pool,
        "holder": card.lease.holder,
        "fencing": card.lease.fencing,
        "acquired_at": card.lease.acquired_at,
        "expires_at": card.lease.expires_at,
        "heartbeat_at": card.lease.heartbeat_at,
    }


@get(
    "/api/v1/spaces/{space_slug:str}/accounts/{identity_id:uuid}",
    operation_id="admin_v1_account_get",
    tags=["accounts"],
    opt=route_meta("viewer", "operator", "admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 503),
)
async def account_detail(
    request: Request[Any, Any, Any], space_slug: FromPath[str], identity_id: FromPath[UUID]
) -> ResourceEnvelope[AccountHeaderResponse]:
    card = await _card(request, space_slug, identity_id)
    row = card.identity
    return ResourceEnvelope(
        data=AccountHeaderResponse(
            id=str(row.id),
            short_id=row.id.hex[-8:],
            space=space_slug,
            service=row.service,
            external_ref=row.external_ref,
            lifecycle=row.lifecycle,
            standing=row.standing,
            auth=row.auth_state,
            availability=row.availability,
            eligible=card.eligible,
            blockers=list(card.blockers),
            revision=card.revision,
            created_at=row.created_at,
            updated_at=row.updated_at,
            lease=_lease(card),
        )
    )


def _stats_payload(value: Any) -> dict[str, Any]:
    return {
        "since": value.since,
        "count": value.count,
        "total_seconds": value.total.total_seconds(),
        "average_seconds": value.average.total_seconds() if value.average else None,
        "longest_seconds": value.longest.total_seconds() if value.longest else None,
        "utilization": value.utilization,
        "by_reason": value.by_reason,
        "operations": value.operations,
        "succeeded": value.succeeded,
        "failed": value.failed,
    }


async def _section_data(
    handle: SpaceHandle, card: IdentityCard, section: AccountSection, role: AdminRole
) -> AccountSectionResponse:
    identity_id = card.identity.id
    if section == "state":
        return AccountSectionResponse(
            section=section,
            revision=card.revision,
            data={"regions": [asdict(region) for region in card.regions]},
            actions=_state_actions(handle, card, role),
        )
    if section == "profile":
        row = card.identity
        service = handle.hub.service(row.service)
        return AccountSectionResponse(
            section=section,
            revision=card.revision,
            data={
                "profile": dict(row.profile),
                "attributes": dict(row.attributes),
                "labels": dict(row.labels),
                "tags": list(row.tags),
                "note": row.note,
                "data_namespaces": [asdict(item) for item in card.data_namespaces],
                "namespace_contents_available": False,
                "namespace_contents_reason": (
                    "Схема содержимого принадлежит приложению-владельцу namespace."
                ),
            },
            secrets=(
                [SecretDescriptorResponse(field="profile", label="Зашифрованная часть профиля")]
                if role == AdminRole.ADMIN and service.profile_encrypted
                else []
            ),
        )
    if section == "access":
        access = await handle.access_card(identity_id)
        access_secrets: list[SecretDescriptorResponse] = []
        if role == AdminRole.ADMIN:
            if access.credential.present:
                access_secrets.append(
                    SecretDescriptorResponse(
                        field="credentials",
                        label="Credentials",
                        update_kind="credentials",
                    )
                )
            access_secrets.extend(
                SecretDescriptorResponse(
                    field=f"session:{item.slot}",
                    label=f"Session · {item.slot}",
                    update_kind="session",
                )
                for item in access.sessions
            )
        return AccountSectionResponse(
            section=section,
            revision=card.revision,
            data={
                "credential": asdict(access.credential),
                "sessions": [asdict(item) for item in access.sessions],
                "secret_values_included": False,
            },
            secrets=access_secrets,
        )
    if section == "verification":
        service = handle.hub.service(card.identity.service)
        factors = {item.factor: item for item in card.verifications}
        now = handle.hub.clock.now()
        actions: list[AccountActionResponse] = []
        if role != AdminRole.VIEWER:
            if service.authenticator is not None and card.identity.auth_state in {
                "no_session",
                "session_invalid",
            }:
                actions.append(
                    AccountActionResponse(
                        id="run-login",
                        kind="run_login",
                        label="Запустить вход",
                        required_role="operator",
                        region="auth",
                        consequence_codes=["uses_login_lease", "may_open_challenge"],
                    )
                )
            if service.probes is not None:
                actions.append(
                    AccountActionResponse(
                        id="resync:auth",
                        kind="resync",
                        label="Перепроверить вход",
                        required_role="operator",
                        region="auth",
                        key="auth",
                        consequence_codes=["calls_platform_probe"],
                    )
                )
            challenge = card.open_challenge
            if challenge is not None and (
                challenge.get("expires_at") is None or challenge["expires_at"] > now
            ):
                actions.append(
                    AccountActionResponse(
                        id=f"answer-challenge:{challenge['id']}",
                        kind="answer_challenge",
                        label="Ответить на challenge",
                        required_role="operator",
                        region="auth",
                        key=str(challenge["id"]),
                        consequence_codes=["answer_is_encrypted", "login_continues_immediately"],
                    )
                )
            for factor, item in sorted(factors.items()):
                spec = service.verification(factor)
                if item.state == "under_review" and str(spec.mode) == "manual":
                    actions.extend(
                        [
                            AccountActionResponse(
                                id=f"verification-approve:{factor}",
                                kind="verification_approve",
                                label=f"Одобрить {factor}",
                                required_role="operator",
                                region="verification",
                                key=factor,
                                requires_confirmation=True,
                                consequence_codes=["factor_becomes_verified", "may_reach_tier"],
                            ),
                            AccountActionResponse(
                                id=f"verification-reject:{factor}",
                                kind="verification_reject",
                                label=f"Отклонить {factor}",
                                required_role="operator",
                                region="verification",
                                key=factor,
                                allowed_targets=sorted(spec.reject_codes),
                                requires_confirmation=True,
                                consequence_codes=["factor_becomes_rejected"],
                            ),
                        ]
                    )
                if item.state == "pending":
                    actions.append(
                        AccountActionResponse(
                            id=f"verification-abandon:{factor}",
                            kind="verification_abandon",
                            label=f"Отменить {factor}",
                            required_role="operator",
                            region="verification",
                            key=factor,
                            requires_confirmation=True,
                            consequence_codes=["pending_evidence_is_not_revealed"],
                        )
                    )
                if service.probes is not None:
                    actions.append(
                        AccountActionResponse(
                            id=f"resync:verification:{factor}",
                            kind="resync",
                            label=f"Перепроверить {factor}",
                            required_role="operator",
                            region="verification",
                            key=f"verification:{factor}",
                            consequence_codes=["calls_platform_probe"],
                        )
                    )
            actions.extend(
                AccountActionResponse(
                    id=f"resolve-requirement:{item['code']}",
                    kind="resolve_requirement",
                    label=f"Разрешить требование {item['code']}",
                    required_role="operator",
                    key=str(item["code"]),
                    consequence_codes=["removes_human_queue_item"],
                )
                for item in card.identity.requirements
                if isinstance(item, dict) and isinstance(item.get("code"), str)
            )
        return AccountSectionResponse(
            section=section,
            revision=card.revision,
            data={
                "factors": [asdict(item) for item in card.verifications],
                "factor_specs": [
                    {
                        "name": spec.name,
                        "requires": sorted(spec.requires),
                        "max_attempts": spec.max_attempts,
                        "mode": spec.mode,
                        "reject_codes": sorted(spec.reject_codes),
                        "instruction": (
                            "Ручное решение принимает оператор после проверки данных сервиса."
                            if str(spec.mode) == "manual"
                            else "Результат подтверждает платформа."
                        ),
                    }
                    for spec in service.verifications
                ],
                "tiers": [
                    {
                        "name": tier.name,
                        "factors": sorted(tier.factors),
                        "achieved": all(
                            factor in factors and factors[factor].state == "verified"
                            for factor in tier.factors
                        ),
                        "missing": sorted(
                            factor
                            for factor in tier.factors
                            if factor not in factors or factors[factor].state != "verified"
                        ),
                    }
                    for tier in service.tiers
                ],
                "requirements": [item for item in card.identity.requirements if isinstance(item, dict)],
                "challenge": (
                    {
                        **card.open_challenge,
                        "expired": (
                            card.open_challenge.get("expires_at") is not None
                            and card.open_challenge["expires_at"] <= now
                        ),
                    }
                    if card.open_challenge is not None
                    else None
                ),
            },
            actions=actions,
            secrets=(
                [
                    SecretDescriptorResponse(
                        field=f"evidence:{item.factor}",
                        label=f"Evidence · {item.factor}",
                    )
                    for item in card.verifications
                    if item.has_evidence
                ]
                if role == AdminRole.ADMIN
                else []
            ),
        )
    if section == "leases":
        history = await handle.lease_history(identity_id, limit=50)
        now = datetime.now(UTC)
        stats = {
            label: _stats_payload(await handle.lease_stats(identity_id, now - delta))
            for label, delta in (
                ("24h", timedelta(days=1)),
                ("7d", timedelta(days=7)),
                ("30d", timedelta(days=30)),
            )
        }
        service = handle.hub.service(card.identity.service)
        quota_limits = {
            (kind, quota.window_seconds): quota.limit
            for kind, rule in service.operations.items()
            for quota in rule.quotas
        }
        lease_actions: list[AccountActionResponse] = []
        if card.lease is not None and role != AdminRole.VIEWER:
            lease_actions.append(
                AccountActionResponse(
                    id="lease:revoke",
                    kind="lease_revoke",
                    label="Отозвать текущую аренду",
                    required_role="operator",
                    requires_confirmation=True,
                    consequence_codes=["increments_fencing", "worker_loses_lease"],
                )
            )
        return AccountSectionResponse(
            section=section,
            revision=card.revision,
            data={
                "current": _lease(card),
                "history": [asdict(item) for item in history],
                "stats": stats,
                "quotas": [
                    {
                        **asdict(item),
                        "limit": quota_limits.get((item.kind, item.window_seconds)),
                        "reset_at": item.window_start + timedelta(seconds=item.window_seconds),
                    }
                    for item in card.quotas
                ],
            },
            actions=lease_actions,
        )
    if section == "network":
        impact = await handle.impact_of(identity_id)
        network_actions: list[AccountActionResponse] = []
        if role != AdminRole.VIEWER:
            network_actions.append(
                AccountActionResponse(
                    id="link:create",
                    kind="link",
                    label="Добавить связь",
                    required_role="operator",
                    requires_role_input=True,
                    consequence_codes=["changes_dependency_graph"],
                )
            )
            network_actions.extend(
                AccountActionResponse(
                    id=f"link:unlink:{item.role}",
                    kind="unlink",
                    label=f"Удалить связь: {item.role}",
                    required_role="operator",
                    key=item.role,
                    target=str(item.identity_id),
                    requires_confirmation=bool(impact),
                    consequence_codes=["affects_dependents"] if impact else ["changes_dependency_graph"],
                )
                for item in card.links.outgoing
            )
            proxies = await handle.proxy_overview(limit=200)
            targets = [
                str(item.id)
                for item in proxies.items
                if item.status == "alive"
                and item.available_capacity > 0
                and (card.proxy is None or item.id != card.proxy.id)
            ]
            if targets:
                network_actions.append(
                    AccountActionResponse(
                        id="proxy:rebind",
                        kind="proxy_rebind",
                        label="Перепривязать прокси",
                        required_role="operator",
                        allowed_targets=targets,
                        requires_confirmation=True,
                        consequence_codes=["changes_network_identity", "consumes_proxy_capacity"],
                    )
                )
        return AccountSectionResponse(
            section=section,
            revision=card.revision,
            data={
                "proxy": asdict(card.proxy) if card.proxy else None,
                "outgoing": [asdict(item) for item in card.links.outgoing],
                "incoming": [asdict(item) for item in card.links.incoming],
                "impact": [asdict(item) for item in impact],
            },
            actions=network_actions,
        )
    return AccountSectionResponse(
        section=section,
        revision=card.revision,
        available=False,
        reason="Публичная page-read модель появится в A2.7/A4.1.",
        data={"items": []},
    )


def _state_actions(handle: SpaceHandle, card: IdentityCard, role: AdminRole) -> list[AccountActionResponse]:
    """Derive every executable state action from the registered domain machines."""
    service = handle.hub.service(card.identity.service)
    actions: list[AccountActionResponse] = []
    for region in card.regions:
        spec = service.region_spec(region.region)
        if region.region == "lifecycle":
            for target in sorted(spec.transitions.get(region.state, frozenset())):
                retiring = target == "retired"
                actions.append(
                    AccountActionResponse(
                        id=f"lifecycle:{target}",
                        kind="lifecycle",
                        label="Вывести из парка" if retiring else "Вернуть в парк",
                        required_role="operator",
                        region="lifecycle",
                        target=target,
                        requires_confirmation=retiring,
                        consequence_codes=["removes_from_lease_pool"]
                        if retiring
                        else ["returns_to_lease_pool"],
                    )
                )
        if region.region == "standing" and region.state == "banned":
            actions.append(
                AccountActionResponse(
                    id="standing:restore",
                    kind="restore",
                    label="Восстановить после блокировки",
                    required_role="operator",
                    region="standing",
                    target="good",
                    requires_confirmation=True,
                    consequence_codes=["clears_sticky_state", "can_return_to_lease_pool"],
                )
            )
        actions.append(
            AccountActionResponse(
                id=f"override:{region.region}:{region.key}",
                kind="override",
                label=f"Исправить вручную: {region.region}{':' + region.key if region.key else ''}",
                required_role="admin",
                region=region.region,
                key=region.key,
                allowed_targets=sorted(spec.states),
                requires_confirmation=True,
                consequence_codes=(["overrides_sticky_state"] if region.state in spec.sticky else [])
                + (["can_mask_platform_observations"] if spec.synced else []),
                pin_supported=spec.synced,
            )
        )
        if region.pinned_until is not None:
            actions.append(
                AccountActionResponse(
                    id=f"unpin:{region.region}:{region.key}",
                    kind="unpin",
                    label=f"Снять закрепление: {region.region}{':' + region.key if region.key else ''}",
                    required_role="admin",
                    region=region.region,
                    key=region.key,
                    requires_confirmation=True,
                    consequence_codes=["platform_observations_can_apply"],
                )
            )
    if role == AdminRole.VIEWER:
        return []
    if role == AdminRole.OPERATOR:
        return [action for action in actions if action.required_role == "operator"]
    return actions


@get(
    "/api/v1/spaces/{space_slug:str}/accounts/{identity_id:uuid}/{section:str}",
    operation_id="admin_v1_account_section_get",
    tags=["accounts"],
    opt=route_meta("viewer", "operator", "admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 503),
)
async def account_section(
    request: Request[AdminPrincipal, Any, Any],
    space_slug: FromPath[str],
    identity_id: FromPath[UUID],
    section: FromPath[AccountSection],
) -> ResourceEnvelope[AccountSectionResponse]:
    handle = _handle(request, space_slug)
    card = await _card(request, space_slug, identity_id)
    return ResourceEnvelope(data=await _section_data(handle, card, section, request.user.role))


ACCOUNT_DETAIL_ROUTE_HANDLERS = [account_detail, account_section]
