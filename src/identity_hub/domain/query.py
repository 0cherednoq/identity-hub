"""Парсер строки поиска админки (FR-11.1, 11.3).

    service:instagram status:active country:UA farm:3 olena "две слова"

- `service:` — сервис; `lifecycle: standing: auth: availability:` — регионы; `status:` — D-105;
- `tag:` — тег; прочие `ключ:значение` — фильтры, разрешаемые в profile/labels при поиске;
- полный UUID — точный поиск по id; `id:<hex>` — по окончанию id без дефисов;
- прочие слова — подстрочный поиск, короче 3 символов игнорируются;
- строка длиннее `MAX_QUERY_LENGTH` или с числом условий больше `MAX_QUERY_TOKENS` — `ValidationFailed`.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from identity_hub.enums import AuthState, Availability, Lifecycle, Standing
from identity_hub.errors import ValidationFailed

__all__ = [
    "MAX_QUERY_LENGTH",
    "MAX_QUERY_TOKENS",
    "MIN_TERM_LENGTH",
    "REGION_KEYS",
    "SearchQuery",
    "parse_query",
    "profile_sort_text",
]

MIN_TERM_LENGTH = 3
# каждое слово запроса — отдельное условие ILIKE: без предела запрос из сотен слов нагружает базу
MAX_QUERY_LENGTH = 1000
MAX_QUERY_TOKENS = 50
REGION_KEYS = {
    "lifecycle": frozenset(Lifecycle),
    "standing": frozenset(Standing),
    "auth": frozenset(AuthState),
    "availability": frozenset(Availability),
}
STATUS_ALIASES: dict[str, dict[str, frozenset[str]]] = {
    "active": {
        "lifecycle": frozenset({Lifecycle.READY}),
        "standing": frozenset({Standing.GOOD, Standing.LIMITED}),
    },
}
_TOKEN = re.compile(r"^([A-Za-z0-9_.\-]+):(.+)$")


@dataclass(frozen=True)
class SearchQuery:
    services: frozenset[str] = frozenset()
    regions: dict[str, frozenset[str]] = field(default_factory=dict)
    any_region: frozenset[str] = frozenset()  # status:<значение> без псевдонима — в любом регионе
    tags: frozenset[str] = frozenset()
    filters: dict[str, frozenset[str]] = field(default_factory=dict)  # profile или labels
    terms: tuple[str, ...] = ()
    exact_id: UUID | None = None
    short_id: str | None = None

    @property
    def is_empty(self) -> bool:
        return not (
            self.services
            or self.regions
            or self.any_region
            or self.tags
            or self.filters
            or self.terms
            or self.exact_id
            or self.short_id
        )


def _tokens(text: str) -> list[str]:
    try:
        return shlex.split(text, posix=True)
    except ValueError:  # незакрытая кавычка — разбираем по пробелам
        return text.replace('"', " ").replace("'", " ").split()


def profile_sort_text(value: Any) -> str | None:
    """Значение поля профиля для сортировки: текст в том же виде, что `profile ->> 'key'` в PostgreSQL."""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(value, ensure_ascii=False)


def parse_query(text: str) -> SearchQuery:
    if len(text) > MAX_QUERY_LENGTH:
        raise ValidationFailed(f"строка поиска длиннее {MAX_QUERY_LENGTH} символов")
    tokens = _tokens(text)
    if len(tokens) > MAX_QUERY_TOKENS:
        raise ValidationFailed(f"в строке поиска больше {MAX_QUERY_TOKENS} условий")
    services: set[str] = set()
    regions: dict[str, set[str]] = {}
    any_region: set[str] = set()
    tags: set[str] = set()
    filters: dict[str, set[str]] = {}
    terms: list[str] = []
    exact_id: UUID | None = None
    short_id: str | None = None

    for token in tokens:
        match = _TOKEN.match(token)
        if match is None:
            parsed = _as_uuid(token)
            if parsed is not None:
                exact_id = parsed
            elif len(token) >= MIN_TERM_LENGTH:
                terms.append(token.lower())
            continue
        key, value = match.group(1).lower(), match.group(2)
        if key == "service":
            services.add(value.lower())
        elif key == "tag":
            tags.add(value)
        elif key in REGION_KEYS:
            regions.setdefault(key, set()).add(value.lower())
        elif key == "status":
            alias = STATUS_ALIASES.get(value.lower())
            if alias is None:
                any_region.add(value.lower())
            else:
                for region, values in alias.items():
                    regions.setdefault(region, set()).update(values)
        elif key == "id":
            parsed = _as_uuid(value)
            if parsed is not None:
                exact_id = parsed
            elif len(value) >= MIN_TERM_LENGTH and re.fullmatch(r"[0-9a-fA-F]+", value):
                short_id = value.lower()
        else:
            filters.setdefault(match.group(1), set()).add(value)

    return SearchQuery(
        services=frozenset(services),
        regions={k: frozenset(v) for k, v in regions.items()},
        any_region=frozenset(any_region),
        tags=frozenset(tags),
        filters={k: frozenset(v) for k, v in filters.items()},
        terms=tuple(dict.fromkeys(terms)),
        exact_id=exact_id,
        short_id=short_id,
    )


def _as_uuid(value: str) -> UUID | None:
    if len(value) not in (32, 36):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None
