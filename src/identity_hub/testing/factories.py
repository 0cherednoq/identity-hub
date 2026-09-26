"""Фабрики компактных сервисов и готовых к аренде аккаунтов для тестов."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from identity_hub.enums import AuthState, Lifecycle
from identity_hub.hub import SpaceHandle
from identity_hub.service import OperationRule, Service

__all__ = ["TestingModel", "make_identity", "make_service"]


class TestingModel(BaseModel):
    """Разрешает произвольные поля, чтобы тест не объявлял Pydantic-схему ради одной записи."""

    model_config = ConfigDict(extra="allow")


def make_service(
    name: str = "test_service",
    title: str | None = None,
    *,
    operations: Mapping[str, OperationRule] | None = None,
    attributes: type[BaseModel] = TestingModel,
    profile: type[BaseModel] = TestingModel,
    credentials: type[BaseModel] = TestingModel,
    session: type[BaseModel] = TestingModel,
    **overrides: Any,
) -> Service:
    """Создать минимальный Service; операция ``work`` доступна по умолчанию."""

    return Service(
        name=name,
        title=title or name.replace("_", " ").title(),
        attributes=attributes,
        profile=profile,
        credentials=credentials,
        session=session,
        operations=operations or {"work": OperationRule()},
        **overrides,
    )


async def make_identity(
    space: SpaceHandle,
    *,
    service: Service | str = "test_service",
    external_ref: str | None = None,
    identity_id: UUID | None = None,
    credentials: BaseModel | Mapping[str, Any] | None = None,
    session: BaseModel | Mapping[str, Any] | None = None,
    attributes: BaseModel | Mapping[str, Any] | None = None,
    profile: BaseModel | Mapping[str, Any] | None = None,
    labels: Mapping[str, Any] | None = None,
    tags: Iterable[str] = (),
    note: str | None = None,
    links: Mapping[str, UUID] | None = None,
    lifecycle: Lifecycle | str = Lifecycle.READY,
    authenticated: bool = True,
    actor: str = "test",
) -> UUID:
    """Зарегистрировать аккаунт и вернуть UUID.

    По умолчанию аккаунт сразу ``ready`` и ``authenticated``, поэтому его можно взять из пула.
    Передайте ``authenticated=False`` для сценария входа или явно задайте ``session``.
    """

    if external_ref is None:
        external_ref = f"test-{uuid4()}"
    effective_session = session
    auth_state: AuthState | None = None
    if authenticated:
        effective_session = {} if session is None else session
    elif session is None:
        auth_state = AuthState.NO_SESSION
    result = await space.register(
        service=service,
        external_ref=external_ref,
        id=identity_id,
        credentials=credentials,
        session=effective_session,
        attributes=attributes,
        profile=profile,
        labels=labels,
        tags=tags,
        note=note,
        links=links,
        lifecycle=lifecycle,
        auth_state=auth_state,
        actor=actor,
    )
    return result.identity_id
