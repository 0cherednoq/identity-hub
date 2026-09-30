"""Поиск аккаунтов для админки (ТЗ FR-11.1–11.4).

Строка `service:instagram status:active country:UA farm:3 olena` разбирается `parse_query`;
ключ из `filterable_profile` сервиса идёт в `profile @>`, иначе — в `labels @>` (D-105).
Пагинация — keyset `(created_at, id)`, счётчик ограничен 1000 («1000+»).
"""

from __future__ import annotations

import base64
import contextlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from identity_hub._space import SpaceBase
from identity_hub.api._base import legacy
from identity_hub.domain.query import SearchQuery, parse_query
from identity_hub.errors import ValidationFailed
from identity_hub.service import Service
from identity_hub.store.base import IdentityRow, SearchFilter

__all__ = [
    "COUNT_LIMIT",
    "AccountLeaseInfo",
    "AccountListItem",
    "AccountPage",
    "AccountProxyInfo",
    "AccountQuotaInfo",
    "AccountSearchOptions",
    "AccountSelectionEntry",
    "AccountSelectionSnapshot",
    "SearchMixin",
    "SearchPage",
    "decode_cursor",
    "encode_cursor",
]

COUNT_LIMIT = 1000
MAX_PAGE = 500
REGION_COLUMNS = {
    "lifecycle": "lifecycle",
    "standing": "standing",
    "auth": "auth_state",
    "availability": "availability",
}
ACCOUNT_SORT_FIELDS = {
    "auth": "auth_state",
    "availability": "availability",
    "created_at": "created_at",
    "eligible": "eligible",
    "error_score": "error_score",
    "external_ref": "external_ref",
    "last_leased_at": "last_leased_at",
    "leased": "leased",
    "lifecycle": "lifecycle",
    "service": "service",
    "standing": "standing",
    "synced_at": "synced_at",
    "verification_tier": "verification_tier",
}


@dataclass(frozen=True, slots=True)
class SearchPage:
    items: list[IdentityRow]
    next_cursor: str | None
    total: int | None  # None — счётчик не запрашивался; COUNT_LIMIT + 1 — «больше 1000»

    @property
    def total_label(self) -> str | None:
        if self.total is None:
            return None
        return f"{COUNT_LIMIT}+" if self.total > COUNT_LIMIT else str(self.total)


@dataclass(frozen=True, slots=True)
class AccountSearchOptions:
    """Structured admin filters that do not fit the free-form search grammar."""

    attention: bool | None = None
    eligible: bool | None = None
    leased: bool | None = None
    has_override: bool | None = None
    requirement: str | None = None
    factor: str | None = None
    factor_state: str | None = None
    tier: str | None = None
    proxy: str | None = None
    linked: bool | None = None
    quota_kind: str | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None
    last_leased_from: datetime | None = None
    last_leased_to: datetime | None = None
    synced_from: datetime | None = None
    synced_to: datetime | None = None
    standing_changed_from: datetime | None = None
    standing_changed_to: datetime | None = None
    sort_by: str = "created_at"
    sort_desc: bool = True


@dataclass(frozen=True, slots=True)
class AccountLeaseInfo:
    pool: str
    holder: str
    acquired_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class AccountProxyInfo:
    id: UUID
    host: str
    port: int
    status: str


@dataclass(frozen=True, slots=True)
class AccountQuotaInfo:
    kind: str
    window_seconds: int
    used: int
    limit: int
    reset_at: datetime


@dataclass(frozen=True, slots=True)
class AccountListItem:
    """Safe, secret-free row for account list UIs."""

    id: UUID
    service: str
    external_ref: str
    lifecycle: str
    standing: str
    auth: str
    availability: str
    eligible: bool
    blockers: tuple[str, ...]
    lease: AccountLeaseInfo | None
    proxy: AccountProxyInfo | None
    verification_tier: str | None
    verification_tier_rank: int | None
    requirements: tuple[str, ...]
    tags: tuple[str, ...]
    labels: dict[str, Any]
    profile: dict[str, Any]
    has_override: bool
    last_leased_at: datetime | None
    last_synced_at: datetime | None
    error_score: float
    outgoing_links: int
    incoming_links: int
    quotas: tuple[AccountQuotaInfo, ...]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class AccountPage:
    items: tuple[AccountListItem, ...]
    next_cursor: str | None
    total: int | None

    @property
    def total_label(self) -> str | None:
        if self.total is None:
            return None
        return f"{COUNT_LIMIT}+" if self.total > COUNT_LIMIT else str(self.total)


@dataclass(frozen=True, slots=True)
class AccountSelectionEntry:
    id: UUID
    revision: int
    service: str
    external_ref: str


@dataclass(frozen=True, slots=True)
class AccountSelectionSnapshot:
    entries: tuple[AccountSelectionEntry, ...]
    truncated: bool


def encode_cursor(created_at: datetime, identity_id: UUID) -> str:
    raw = f"{created_at.isoformat()}|{identity_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        stamp, _, ident = base64.urlsafe_b64decode(padded).decode().partition("|")
        return datetime.fromisoformat(stamp), UUID(ident)
    except (ValueError, UnicodeDecodeError):
        raise ValidationFailed("некорректный курсор пагинации") from None


def _encode_account_cursor(sort_by: str, sort_desc: bool, value: Any, identity_id: UUID) -> str:
    kind: str
    encoded: Any
    if isinstance(value, datetime):
        kind, encoded = "datetime", value.isoformat()
    elif isinstance(value, bool):
        kind, encoded = "bool", value
    elif isinstance(value, (int, float)):
        kind, encoded = "number", value
    elif value is None:
        kind, encoded = "null", None
    else:
        kind, encoded = "string", str(value)
    raw = json.dumps(
        [1, sort_by, sort_desc, kind, encoded, str(identity_id)],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_account_cursor(cursor: str, expected_sort: str, expected_desc: bool) -> tuple[Any, UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        version, sort_by, sort_desc, kind, encoded, raw_id = json.loads(
            base64.urlsafe_b64decode(padded).decode()
        )
        if version != 1 or sort_by != expected_sort or sort_desc is not expected_desc:
            raise ValueError
        if kind == "datetime":
            value: Any = datetime.fromisoformat(encoded)
        elif (kind == "bool" and isinstance(encoded, bool)) or (
            kind == "number" and isinstance(encoded, (int, float))
        ):
            value = encoded
        elif kind == "null" and encoded is None:
            value = None
        elif kind == "string" and isinstance(encoded, str):
            value = encoded
        else:
            raise ValueError
        return value, UUID(raw_id)
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        raise ValidationFailed("некорректный курсор пагинации") from None


def _label_variants(key: str, value: str) -> tuple[dict[str, Any], ...]:
    """`farm:3` совпадает и с меткой "3", и с числом 3; `vip:true` — и с bool."""
    variants: list[Any] = [value]
    lowered = value.lower()
    if lowered in ("true", "false"):
        variants.append(lowered == "true")
    else:
        try:
            variants.append(int(value))
        except ValueError:
            with contextlib.suppress(ValueError):
                variants.append(float(value))
    return tuple({key: v} for v in variants)


class SearchMixin(SpaceBase):
    async def account_selection_snapshot(
        self,
        query: str | SearchQuery = "",
        *,
        options: AccountSearchOptions | None = None,
        limit: int = 1000,
    ) -> AccountSelectionSnapshot:
        """Freeze safe identifiers and revisions for a bounded server-side bulk selection."""
        if not 1 <= limit <= 5000:
            raise ValidationFailed("selection limit: от 1 до 5000")
        q = parse_query(query) if isinstance(query, str) else query
        flt = self._search_filter(q, options or AccountSearchOptions())
        async with self._tx(own=True) as tx:
            rows, _ = await tx.search_identities(
                flt,
                limit=limit + 1,
                after=None,
                count_limit=None,
            )
        selected = rows[:limit]
        return AccountSelectionSnapshot(
            entries=tuple(
                AccountSelectionEntry(
                    id=row.id,
                    revision=row.event_seq,
                    service=row.service,
                    external_ref=row.external_ref,
                )
                for row in selected
            ),
            truncated=len(rows) > limit,
        )

    async def _search(
        self,
        query: str | SearchQuery = "",
        *,
        limit: int = 50,
        after: str | None = None,
        count: bool = True,
    ) -> SearchPage:
        """Поиск по строке админки (FR-11.1). Секретов в результате нет (FR-5.2)."""
        if not 1 <= limit <= MAX_PAGE:
            raise ValidationFailed(f"limit: от 1 до {MAX_PAGE}")
        q = parse_query(query) if isinstance(query, str) else query
        flt = self._search_filter(q)
        cursor = decode_cursor(after) if after else None
        async with self._tx(own=True) as tx:
            rows, total = await tx.search_identities(
                flt, limit=limit + 1, after=cursor, count_limit=COUNT_LIMIT if count else None
            )
        page = rows[:limit]
        next_cursor = encode_cursor(page[-1].created_at, page[-1].id) if len(rows) > limit else None
        return SearchPage(items=page, next_cursor=next_cursor, total=total)

    search = legacy(_search, "search")

    async def account_search(
        self,
        query: str | SearchQuery = "",
        *,
        options: AccountSearchOptions | None = None,
        limit: int = 50,
        after: str | None = None,
        count: bool = True,
    ) -> AccountPage:
        """Return one enriched, secret-free admin page without loading the full result set."""
        if not 1 <= limit <= MAX_PAGE:
            raise ValidationFailed(f"limit: от 1 до {MAX_PAGE}")
        q = parse_query(query) if isinstance(query, str) else query
        options = options or AccountSearchOptions()
        flt = self._search_filter(q, options)
        cursor = _decode_account_cursor(after, flt.sort_by, flt.sort_desc) if after else None
        async with self._tx(own=True) as tx:
            rows, total = await tx.search_identities(
                flt,
                limit=limit + 1,
                after=cursor,
                count_limit=COUNT_LIMIT if count else None,
            )
            page_rows = rows[:limit]
            link_counts = await tx.link_counts([row.id for row in page_rows])
            now = await tx.now()
            items: list[AccountListItem] = []
            for row in page_rows:
                regions = await tx.list_regions(row.id)
                lease = await tx.current_lease(row.id)
                proxy = await tx.get_binding(row.id)
                history = await tx.lease_history(row.id, limit=1, before=None)
                blockers = _account_blockers(row, now)
                tier = _highest_tier(self.hub.service(row.service), frozenset(row.verified_factors))
                tier_rank = next(
                    (
                        index
                        for index, candidate in enumerate(self.hub.service(row.service).tiers)
                        if candidate.name == tier
                    ),
                    None,
                )
                synced_values = [state.synced_at for _, _, state in regions if state.synced_at is not None]
                outgoing, incoming, _ = link_counts.get(row.id, (0, 0, []))
                quota_items: list[AccountQuotaInfo] = []
                service = self.hub.service(row.service)
                rule = service.operations.get(options.quota_kind) if options.quota_kind else None
                if rule is not None:
                    usage = await tx.quota_usage(row.id)
                    used_by_window = {
                        (item.kind, item.window_seconds, item.window_start): item.used for item in usage
                    }
                    for quota in rule.quotas:
                        window_start = quota.window_start(now)
                        quota_items.append(
                            AccountQuotaInfo(
                                options.quota_kind or "",
                                quota.window_seconds,
                                used_by_window.get(
                                    (options.quota_kind or "", quota.window_seconds, window_start), 0
                                ),
                                quota.limit,
                                window_start + quota.per,
                            )
                        )
                items.append(
                    AccountListItem(
                        id=row.id,
                        service=row.service,
                        external_ref=row.external_ref,
                        lifecycle=row.lifecycle,
                        standing=row.standing,
                        auth=row.auth_state,
                        availability=row.availability,
                        eligible=not blockers,
                        blockers=blockers,
                        lease=(
                            AccountLeaseInfo(
                                lease.pool,
                                lease.holder,
                                lease.acquired_at,
                                lease.expires_at,
                            )
                            if lease is not None
                            else None
                        ),
                        proxy=(
                            AccountProxyInfo(proxy.id, proxy.host, proxy.port, proxy.status)
                            if proxy is not None
                            else None
                        ),
                        verification_tier=tier,
                        verification_tier_rank=tier_rank,
                        requirements=tuple(
                            sorted(
                                {
                                    str(item["code"])
                                    for item in row.requirements
                                    if isinstance(item, dict) and isinstance(item.get("code"), str)
                                }
                            )
                        ),
                        tags=tuple(row.tags),
                        labels=dict(row.labels),
                        profile=dict(row.profile),
                        has_override=any(
                            state.pinned_until is not None and state.pinned_until > now
                            for _, _, state in regions
                        ),
                        last_leased_at=history[0].acquired_at if history else None,
                        last_synced_at=max(synced_values, default=None),
                        error_score=row.error_score,
                        outgoing_links=outgoing,
                        incoming_links=incoming,
                        quotas=tuple(quota_items),
                        created_at=row.created_at,
                        updated_at=row.updated_at,
                    )
                )
        next_cursor = (
            _encode_account_cursor(
                flt.sort_by,
                flt.sort_desc,
                _account_sort_value(items[-1], flt.sort_by),
                items[-1].id,
            )
            if len(rows) > limit
            else None
        )
        return AccountPage(tuple(items), next_cursor, total)

    def _search_filter(self, q: SearchQuery, options: AccountSearchOptions | None = None) -> SearchFilter:
        options = options or AccountSearchOptions()
        services = [s for s in self.hub.services if not q.services or s.name in q.services]
        filterable = set().union(*(s.filterable_profile for s in services)) if services else set()
        contains: list[tuple[str, tuple[dict[str, Any], ...]]] = []
        for key, values in sorted(q.filters.items()):
            if key in filterable:
                contains.append(("profile", tuple({key: v} for v in sorted(values))))
            else:
                variants = tuple(v for value in sorted(values) for v in _label_variants(key, value))
                contains.append(("labels", variants))
        tier_factors: dict[str, frozenset[str]] = {}
        if options.tier is not None:
            tier_factors = {
                service.name: service.tiers_by_name[options.tier].factors
                for service in services
                if options.tier in service.tiers_by_name
            }
            if not tier_factors:
                raise ValidationFailed(f"неизвестный уровень верификации: {options.tier}")
        if options.sort_by.startswith("profile:"):
            profile_key = options.sort_by.partition(":")[2]
            if profile_key not in filterable:
                raise ValidationFailed(f"поле профиля нельзя сортировать: {profile_key}")
            sort_by = f"profile:{profile_key}"
        else:
            sort_by = ACCOUNT_SORT_FIELDS.get(options.sort_by, "")
            if not sort_by:
                raise ValidationFailed(f"неизвестное поле сортировки: {options.sort_by}")
        sort_tiers = (
            {service.name: tuple(tier.factors for tier in service.tiers) for service in services}
            if sort_by == "verification_tier"
            else {}
        )
        return SearchFilter(
            services=q.services,
            regions={REGION_COLUMNS[k]: v for k, v in q.regions.items()},
            any_region=q.any_region,
            tags=q.tags,
            contains=tuple(contains),
            terms=q.terms,
            exact_id=q.exact_id,
            short_id=q.short_id,
            attention=options.attention,
            eligible=options.eligible,
            leased=options.leased,
            has_override=options.has_override,
            requirement=options.requirement,
            factor=options.factor,
            factor_state=options.factor_state,
            tier_factors=tier_factors,
            sort_tiers=sort_tiers,
            proxy=options.proxy,
            linked=options.linked,
            created_from=options.created_from,
            created_to=options.created_to,
            last_leased_from=options.last_leased_from,
            last_leased_to=options.last_leased_to,
            synced_from=options.synced_from,
            synced_to=options.synced_to,
            standing_changed_from=options.standing_changed_from,
            standing_changed_to=options.standing_changed_to,
            sort_by=sort_by,
            sort_desc=options.sort_desc,
        )


def _account_blockers(row: IdentityRow, now: datetime) -> tuple[str, ...]:
    blockers: list[str] = []
    if row.lifecycle != "ready":
        blockers.append(f"lifecycle:{row.lifecycle}")
    if row.standing not in ("good", "limited"):
        blockers.append(f"standing:{row.standing}")
    if row.auth_state != "authenticated":
        blockers.append(f"auth:{row.auth_state}")
    available = row.availability == "available" or (
        row.availability in ("paused", "cooling")
        and row.availability_until is not None
        and row.availability_until <= now
    )
    if not available:
        blockers.append(f"availability:{row.availability}")
    return tuple(blockers)


def _highest_tier(service: Service, factors: frozenset[str]) -> str | None:
    reached = [tier.name for tier in service.tiers if tier.factors <= factors]
    return reached[-1] if reached else None


def _account_sort_value(item: AccountListItem, sort_by: str) -> Any:
    if sort_by.startswith("profile:"):
        return item.profile.get(sort_by.partition(":")[2])
    values: dict[str, Any] = {
        "auth_state": item.auth,
        "availability": item.availability,
        "created_at": item.created_at,
        "eligible": int(item.eligible),
        "error_score": item.error_score,
        "external_ref": item.external_ref,
        "last_leased_at": item.last_leased_at,
        "leased": int(item.lease is not None),
        "lifecycle": item.lifecycle,
        "service": item.service,
        "standing": item.standing,
        "synced_at": item.last_synced_at,
        "verification_tier": item.verification_tier_rank,
    }
    return values[sort_by]
