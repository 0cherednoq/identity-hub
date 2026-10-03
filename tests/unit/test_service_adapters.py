"""F6.3a (R2-F27): адаптеры сервиса проверяются при объявлении `Service`, а не на первом вызове в проде."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

import identity_hub
from identity_hub import ValidationFailed
from identity_hub.operation import classify_error
from identity_hub.protocols import Blame, Fault, ProbeResult
from identity_hub.service import Service

pytestmark = pytest.mark.unit


class _Session(BaseModel):
    token: str = "t"


def _service(**adapters: Any) -> Service:
    return Service(name="svc", title="Svc", session=_Session, **adapters)


def _errors(**adapters: Any) -> str:
    with pytest.raises(ValidationFailed) as exc:
        _service(**adapters)
    return " ".join(str(item) for item in exc.value.errors)


class OnlyLogin:
    async def login(self, ctx: Any) -> BaseModel:
        return _Session()


class SyncLogin:
    def login(self, ctx: Any) -> BaseModel:
        return _Session()


def test_authenticator_needs_only_async_login() -> None:
    assert _service(authenticator=OnlyLogin()).authenticator is not None  # refresh не обязателен
    assert "login" in _errors(authenticator=object())
    assert "async def" in _errors(authenticator=SyncLogin())


async def test_authenticator_may_be_a_function() -> None:
    calls: list[Any] = []

    async def login(ctx: Any) -> BaseModel:
        calls.append(ctx)
        return _Session(token="fn")

    service = _service(authenticator=login)

    session = await service.authenticator.login("ctx")  # type: ignore[union-attr]
    assert session == _Session(token="fn") and calls == ["ctx"]

    def sync_login(ctx: Any) -> BaseModel:
        return _Session()

    assert "async def" in _errors(authenticator=sync_login)


class OnlyAuthProbe:
    async def auth(self, ctx: Any) -> ProbeResult | None:
        return None


class SyncStandingProbe:
    async def auth(self, ctx: Any) -> ProbeResult | None:
        return None

    def standing(self, ctx: Any) -> ProbeResult | None:
        return None


def test_probes_may_implement_any_subset() -> None:
    assert _service(probes=OnlyAuthProbe()).probes is not None
    assert "standing" in _errors(probes=SyncStandingProbe())
    assert "auth" in _errors(probes=object())


def test_error_classifier_may_be_a_mapping() -> None:
    limited = Fault(Blame.IDENTITY, code="limited")
    service = _service(error_classifier={TimeoutError: limited, ConnectionError: Fault(Blame.PROXY)})

    classifier = service.error_classifier
    assert classifier is not None
    assert classifier.classify(TimeoutError()) is limited
    assert classifier.classify(ConnectionResetError()).blame is Blame.PROXY  # подкласс находится по MRO
    assert classifier.classify(ValueError()) is None
    assert "classify" in _errors(error_classifier=object())


def test_classifier_receives_operation_kind_when_it_accepts_it() -> None:
    seen: list[str | None] = []

    class WithKind:
        def classify(self, exc: BaseException, *, kind: str | None = None) -> Fault | None:
            seen.append(kind)
            return None

    class WithoutKind:
        def classify(self, exc: BaseException) -> Fault | None:
            seen.append("no-kind")
            return None

    classify_error(WithKind(), RuntimeError(), kind="send")
    classify_error(WithoutKind(), RuntimeError(), kind="send")

    assert seen == ["send", "no-kind"]


class AsyncListImporter:
    def can_import(self, path: Path) -> bool:
        return True

    async def read(self, path: Path) -> list[Any]:  # корутина, а не генератор: async for не сработает
        return []


class GoodImporter:
    def can_import(self, path: Path) -> bool:
        return True

    async def read(self, path: Path) -> AsyncIterator[Any]:
        for item in ():
            yield item


def test_importer_read_is_checked() -> None:
    assert _service(importers=(GoodImporter(),)).importers
    message = _errors(importers=(AsyncListImporter(),))
    assert "AsyncListImporter" in message and "read" in message
    assert "can_import" in _errors(importers=(object(),))


def test_event_consumer_protocol_is_gone() -> None:
    """Мёртвый протокол: `subscribe` принимает `Callable[[Event], Awaitable[None]]`."""
    assert not hasattr(identity_hub, "EventConsumer")
