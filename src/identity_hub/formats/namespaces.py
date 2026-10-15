"""Типизированные дескрипторы неймспейсов ``model.dump`` и ``Model.load`` (D-314)."""

from __future__ import annotations

from typing import Any, overload

from pydantic import BaseModel

__all__ = ["DumpDescriptor", "DumpNamespace", "LoadDescriptor", "LoadNamespace"]


class DumpNamespace[M: BaseModel]:
    """База неймспейса выгрузки, привязанного к экземпляру модели."""

    def __init__(self, value: M) -> None:
        self._value = value


class LoadNamespace[M: BaseModel]:
    """База неймспейса загрузки, привязанного к классу модели."""

    def __init__(self, model: type[M]) -> None:
        self._model = model

    def __call__(self, data: Any, *, strict: bool = False) -> M:
        return self.json(data, strict=strict)

    def json(self, data: Any, *, strict: bool = False) -> M:
        from identity_hub.formats import json as json_format

        return json_format.load(self._model, data, strict=strict)


class DumpDescriptor[M: BaseModel, N]:
    def __init__(self, namespace: type[N]) -> None:
        self._namespace = namespace

    @overload
    def __get__(self, instance: M, owner: type[M]) -> N: ...

    @overload
    def __get__(self, instance: None, owner: type[M]) -> DumpDescriptor[M, N]: ...

    def __get__(self, instance: M | None, owner: type[M]) -> N | DumpDescriptor[M, N]:
        if instance is None:
            return self
        return self._namespace(instance)  # type: ignore[call-arg]


class LoadDescriptor[M: BaseModel, N]:
    def __init__(self, namespace: type[N]) -> None:
        self._namespace = namespace

    def __get__(self, instance: object | None, owner: type[M]) -> N:
        return self._namespace(owner)  # type: ignore[call-arg]
