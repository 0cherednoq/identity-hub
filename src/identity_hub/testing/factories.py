"""Фабрики компактных сервисов и готовых к аренде аккаунтов для тестов."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from identity_hub.api.identities import IdentityCreate
from identity_hub.enums import AuthState, Lifecycle
from identity_hub.errors import ValidationFailed
from identity_hub.hub import SpaceHandle
from identity_hub.service import OperationRule, Service

__all__ = ["TestingModel", "make_identity", "make_service"]


class TestingModel(BaseModel):
    """Модель данных сервиса из `make_service` по умолчанию: без полей, лишние поля — ошибка.

    Опечатка в поле (`pasword`) не проходит молча (R2-P20). Сервису, которому в тесте нужны
    credentials, сессия или профиль, передайте свою модель: `make_service(credentials=Creds)`.
    """

    model_config = ConfigDict(extra="forbid")


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
    """Создать минимальный Service для теста.

    По умолчанию: операция ``work`` без квот, модели данных — `TestingModel` (без полей),
    ``title`` — из имени. Остальные параметры `Service` передаются через ``overrides``.
    """

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

    В отличие от `IdentityCreate` (по умолчанию ``provisioning`` без сессии), фабрика создаёт
    аккаунт сразу ``ready`` и ``authenticated`` с пустой сессией в слоте ``default`` — его можно
    взять из пула. ``authenticated=False`` — аккаунт без сессии (``no_session``) для сценария
    входа; вместе с ``session`` это противоречие и `ValidationFailed`. ``external_ref`` по
    умолчанию — ``test-<uuid>``; если аккаунт с таким ref уже есть, возвращается его id.
    """

    if not authenticated and session is not None:
        raise ValidationFailed("make_identity: authenticated=False не сочетается с session")
    if external_ref is None:
        external_ref = f"test-{uuid4()}"
    effective_session = session
    auth_state: AuthState | None = None
    if authenticated:
        effective_session = {} if session is None else session
    elif session is None:
        auth_state = AuthState.NO_SESSION
    identity, _created = await space.identities.ensure(
        IdentityCreate(
            service=service,
            external_ref=external_ref,
            id=identity_id,
            credentials=credentials,
            session=effective_session,
            attributes=attributes,
            profile=profile,
            labels=labels or {},
            tags=tuple(tags),
            note=note,
            links=links or {},
            lifecycle=lifecycle,
            auth_state=auth_state,
        ),
        actor=actor,
    )
    return identity.id
