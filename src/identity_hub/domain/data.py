"""Данные аккаунта: валидация меток, тегов, схем сервиса; строка текстового поиска (FR-3, FR-11.3)."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from identity_hub.errors import ValidationFailed
from identity_hub.service import Service

__all__ = [
    "LabelValue",
    "build_search_text",
    "short_id",
    "split_profile",
    "validate_labels",
    "validate_model",
    "validate_tags",
]

LabelValue = str | int | float | bool

_KEY = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_TAG = re.compile(r"^[A-Za-z0-9_.:\-]{1,64}$")
MAX_LABEL_VALUE = 256


def validate_labels(labels: Mapping[str, Any]) -> dict[str, LabelValue]:
    """Плоские пары ключ → строка | число | bool; вложенные структуры запрещены (FR-3.3)."""
    errors: list[str] = []
    result: dict[str, LabelValue] = {}
    for key, value in labels.items():
        if not isinstance(key, str) or not _KEY.match(key):
            errors.append(f"метка {key!r}: ключ — латиница, цифры, _ . -, до 64 символов")
            continue
        if isinstance(value, bool | int | float | str) and not (isinstance(value, float) and value != value):
            if isinstance(value, str) and len(value) > MAX_LABEL_VALUE:
                errors.append(f"метка {key}: значение длиннее {MAX_LABEL_VALUE}")
                continue
            result[key] = value
        else:
            errors.append(f"метка {key}: допустимы только строка, число или bool, не {type(value).__name__}")
    if errors:
        raise ValidationFailed("некорректные метки", errors)
    return result


def validate_tags(tags: Iterable[str]) -> list[str]:
    """Множество флагов (FR-3.4): уникальные, отсортированные."""
    values = set(tags)
    bad = sorted(t for t in values if not isinstance(t, str) or not _TAG.match(t))
    if bad:
        raise ValidationFailed("некорректные теги", [f"тег {t!r}" for t in bad])
    return sorted(values)


def validate_model(schema: type[BaseModel], data: Mapping[str, Any] | BaseModel, what: str) -> BaseModel:
    """Проверка данных схемой сервиса до обращения к БД (FR-2.4, FR-3.1, FR-3.2)."""
    if isinstance(data, schema):
        return data
    raw = data.model_dump() if isinstance(data, BaseModel) else data
    try:
        return schema.model_validate(raw)
    except PydanticValidationError as exc:
        errors = [f"{what}.{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()]
        raise ValidationFailed(f"{what} не соответствует схеме {schema.__name__}", errors) from None


def split_profile(service: Service, profile: BaseModel) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Профиль → (открытая часть в ih_identities.profile, шифруемая часть или None).

    При `profile_encrypted` открыты только фильтруемые поля (FR-3.2).
    """
    data = profile.model_dump(mode="json", exclude_none=True)
    if not service.profile_encrypted:
        return data, None
    open_part = {k: v for k, v in data.items() if k in service.filterable_profile}
    secret = {k: v for k, v in data.items() if k not in service.filterable_profile}
    return open_part, secret


def short_id(identity_id: UUID) -> str:
    """Короткий id для поиска и админки: последние 8 hex-символов (у UUIDv7 начало — время)."""
    return identity_id.hex[-8:]


def build_search_text(
    service: Service,
    identity_id: UUID,
    external_ref: str,
    profile: Mapping[str, Any],
    labels: Mapping[str, LabelValue],
    note: str | None,
) -> str:
    """Денормализованная строка для pg_trgm (FR-11.3). `profile` — открытая часть (D-106)."""
    # UUID lookup has a dedicated exact/id: path. Keeping a random short UUID in
    # free text makes ordinary terms nondeterministically match unrelated rows.
    del identity_id
    parts: list[str] = [external_ref]
    searchable = service.searchable_profile
    if service.profile_encrypted:
        searchable = searchable & service.filterable_profile
    for field_name in sorted(searchable):
        value = profile.get(field_name)
        if value not in (None, ""):
            parts.append(str(value))
    for key in sorted(labels):
        parts.append(f"{key}:{_label_text(labels[key])}")
    if note:
        parts.append(note)
    return " ".join(" ".join(p.split()) for p in parts if p).lower()


def _label_text(value: LabelValue) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
