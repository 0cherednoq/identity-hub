"""F6.2a: единые имена и типы публичного API (R1-22, R1-24, R1-25, R1-27, R1-30)."""

from __future__ import annotations

import inspect

import pytest

from identity_hub import (
    IdentityCreate,
    IdentityHub,
    LeaseUnavailable,
    OnConflict,
    StaticSecretsProvider,
    http_status,
)
from identity_hub.api._base import Namespace
from identity_hub.api.imports import Imports
from identity_hub.pool import Pool
from identity_hub.testing import MemoryStore, make_service

pytestmark = pytest.mark.unit


async def _space() -> object:
    hub = IdentityHub(
        secrets=StaticSecretsProvider({"k": bytes(range(32))}, "k"),
        services=[make_service("svc")],
        store=MemoryStore(),
    )
    await hub.start()
    return await hub.spaces.ensure("s", "S")


def test_identity_create_uses_auth_like_identity_and_filter() -> None:
    """R1-22: поле региона авторизации называется `auth` везде."""
    spec = IdentityCreate(service="svc", external_ref="+1", auth="authenticated")
    assert spec.auth == "authenticated"
    with pytest.raises(TypeError):
        IdentityCreate(service="svc", external_ref="+1", auth_state="authenticated")  # type: ignore[call-arg]


async def test_created_identity_gets_requested_auth() -> None:
    space = await _space()
    identity = await space.identities.create(  # type: ignore[attr-defined]
        IdentityCreate(service="svc", external_ref="+1", auth="no_session"), actor="test"
    )
    assert identity.auth == "no_session"


async def test_proxy_create_many_takes_on_conflict_enum() -> None:
    """R1-24: `on_conflict` у прокси — тот же `OnConflict`, что у identities."""
    space = await _space()
    urls = ["http://10.0.0.1:80", "http://10.0.0.2:80"]
    await space.proxies.create_many(urls[:1], group="g")  # type: ignore[attr-defined]

    result = await space.proxies.create_many(urls, group="g", on_conflict=OnConflict.SKIP)  # type: ignore[attr-defined]

    assert len(result.created) == 1


def test_run_files_has_explicit_parameters_like_run() -> None:
    """R1-25: у `run_files` нет `**options`: опечатка в параметре — `TypeError`, а не тишина."""
    run_files = inspect.signature(Imports.run_files).parameters
    run = inspect.signature(Imports.run).parameters
    assert all(item.kind is not inspect.Parameter.VAR_KEYWORD for item in run_files.values())
    assert set(run) - {"records"} <= set(run_files)


def test_among_is_typed() -> None:
    for method in (Pool.lease, Pool.acquire, Pool.candidate_query):
        annotation = str(inspect.signature(method).parameters["among"].annotation)
        assert "Select" in annotation and "Iterable[UUID]" in annotation and "Callable" in annotation


def test_lease_unavailable_is_service_unavailable_with_retry_hint() -> None:
    """R1-27: нет свободного аккаунта — временная недоступность (503), а не «не найдено»."""
    assert http_status(LeaseUnavailable("нет аккаунта")) == 503
    error = LeaseUnavailable("нет аккаунта", retry_after=5.0)
    assert error.retry_after == 5.0 and error.context == {"retry_after": 5.0}


async def test_webhooks_and_notifications_are_cached_namespaces() -> None:
    """R1-30: как остальные неймспейсы — один объект на пространство."""
    space = await _space()
    assert space.webhooks is space.webhooks  # type: ignore[attr-defined]
    assert space.notifications is space.notifications  # type: ignore[attr-defined]
    assert isinstance(space.webhooks, Namespace) and isinstance(space.notifications, Namespace)  # type: ignore[attr-defined]


def _namespace_methods() -> list[tuple[str, object]]:
    import importlib
    import pkgutil

    import identity_hub.api as api
    from identity_hub.dispatch.notifications import Notifications
    from identity_hub.dispatch.webhooks import Webhooks

    classes = {Webhooks, Notifications}
    for module in pkgutil.iter_modules(api.__path__):
        loaded = importlib.import_module(f"identity_hub.api.{module.name}")
        classes |= {
            item
            for item in vars(loaded).values()
            if inspect.isclass(item) and issubclass(item, Namespace) and item.__module__ == loaded.__name__
        }
    return [
        (f"{cls.__name__}.{name}", function)
        for cls in sorted(classes, key=lambda item: item.__name__)
        for name, function in sorted(vars(cls).items())
        if not name.startswith("_") and inspect.iscoroutinefunction(function)
    ]


def test_actor_is_required_keyword_everywhere() -> None:
    """R1-23: где метод принимает `actor`, он обязателен и передаётся по имени — без молчаливых "system"."""
    optional = [
        name
        for name, function in _namespace_methods()
        if (actor := inspect.signature(function).parameters.get("actor")) is not None  # type: ignore[arg-type]
        and (actor.default is not inspect.Parameter.empty or actor.kind is not inspect.Parameter.KEYWORD_ONLY)
    ]
    assert optional == []


async def test_refresh_and_recheck_return_dto() -> None:
    """R1-26: результаты — DTO, как у остальных методов."""
    from identity_hub import RecheckReport, RefreshResult

    assert inspect.signature(Imports.run).return_annotation == "ImportReport"
    from identity_hub.api.proxies import Proxies
    from identity_hub.api.sync import Sync

    assert inspect.signature(Sync.refresh).return_annotation == "RefreshResult"
    assert inspect.signature(Proxies.recheck_due).return_annotation == "RecheckReport"
    assert RefreshResult.__dataclass_params__.frozen and RecheckReport.__dataclass_params__.frozen  # type: ignore[attr-defined]
