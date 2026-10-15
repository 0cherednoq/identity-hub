"""Каноническое JSON-представление моделей стандартных форматов."""

from __future__ import annotations

from typing import Any

import pydantic_core
from pydantic import BaseModel

from identity_hub.crypto import reveal
from identity_hub.errors import ValidationFailed

__all__ = ["dump", "load"]


def dump(value: BaseModel, *, strict: bool = False) -> dict[str, Any] | list[Any]:
    del strict  # JSON — канонический формат без потерь.
    result: Any = pydantic_core.from_json(pydantic_core.to_json(reveal(value)))
    if not isinstance(result, dict | list):  # pragma: no cover - модели дают объект или массив
        raise ValidationFailed("JSON модели должен быть объектом или массивом")
    return result


def load[M: BaseModel](model: type[M], data: Any, *, strict: bool = False) -> M:
    del strict
    try:
        if isinstance(data, str | bytes | bytearray):
            return model.model_validate_json(data)
        return model.model_validate(data)
    except (ValueError, TypeError) as exc:
        raise ValidationFailed(f"JSON не соответствует {model.__name__}: {type(exc).__name__}") from None
