"""Поиск аккаунтов для админки (ТЗ FR-11.1–11.4).

Строка `service:instagram status:active country:UA farm:3 olena` разбирается `parse_query`;
ключ из `filterable_profile` сервиса идёт в `profile @>`, иначе — в `labels @>` (D-105).
Пагинация — keyset `(created_at, id)`, счётчик ограничен 1000 («1000+»).
"""

from __future__ import annotations

import base64
import contextlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from identity_hub._space import SpaceBase
from identity_hub.domain.query import SearchQuery, parse_query
from identity_hub.errors import ValidationFailed
from identity_hub.store.base import IdentityRow, SearchFilter

__all__ = ["COUNT_LIMIT", "SearchMixin", "SearchPage", "decode_cursor", "encode_cursor"]

COUNT_LIMIT = 1000
MAX_PAGE = 500
REGION_COLUMNS = {
    "lifecycle": "lifecycle",
    "standing": "standing",
    "auth": "auth_state",
    "availability": "availability",
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
    async def search(
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

    def _search_filter(self, q: SearchQuery) -> SearchFilter:
        services = [s for s in self.hub.services if not q.services or s.name in q.services]
        filterable = set().union(*(s.filterable_profile for s in services)) if services else set()
        contains: list[tuple[str, tuple[dict[str, Any], ...]]] = []
        for key, values in sorted(q.filters.items()):
            if key in filterable:
                contains.append(("profile", tuple({key: v} for v in sorted(values))))
            else:
                variants = tuple(v for value in sorted(values) for v in _label_variants(key, value))
                contains.append(("labels", variants))
        return SearchFilter(
            services=q.services,
            regions={REGION_COLUMNS[k]: v for k, v in q.regions.items()},
            any_region=q.any_region,
            tags=q.tags,
            contains=tuple(contains),
            terms=q.terms,
            exact_id=q.exact_id,
        )
