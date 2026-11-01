"""`space.auth` — явный вход через `Authenticator` сервиса и ответ на challenge (IMP-23).

Внутри обычной аренды библиотека не логинит: вход запускается только этими методами.
"""

from __future__ import annotations

from uuid import UUID

from identity_hub.api._base import Namespace
from identity_hub.auth import (
    LoginOutcome,
    LoginReport,
    SessionAdoptionResult,
    SessionVerificationResult,
)
from identity_hub.revalidation import AuthRevalidationResult
from identity_hub.service import Service

__all__ = ["Auth"]


class Auth(Namespace):
    """Вход identity: `space.auth`."""

    __slots__ = ()

    async def verify_session(
        self,
        identity_id: UUID,
        slot: str = "default",
        *,
        actor: str,
        expected_revision: int | None = None,
    ) -> SessionVerificationResult:
        """Сетевой verdict slot; обновляет validity metadata, invalid `default` также меняет auth."""
        return await self._at(identity_id, expected_revision)._verify_session(
            identity_id,
            slot,
            actor=actor,
        )

    async def revalidate(
        self,
        identity_id: UUID,
        *,
        actor: str,
        reason: str,
        request_id: str,
        credential: tuple[str, str] | None = None,
        expected_revision: int | None = None,
    ) -> AuthRevalidationResult:
        """Явная перепроверка аккаунта из любого допустимого состояния (D-335).

        Не меняет `lifecycle`, `availability` и `standing` командой, не переводит `auth` в `logging_in`;
        вердикт провайдера применяется как наблюдение, итог показывает, что именно применено.
        `credential=(kind, name)` включает шаг credential: если сессии нет или она невалидна, вызывается
        аутентификатор с этой записью (без `logging_in`); активный challenge возвращается, а не
        перезапускается. Допуск задаёт `Service.diagnostics`. Отказ допуска — `status=blocked` с
        `blockers`, занятый аккаунт — `status=in_progress`, технический сбой — `status=failed`
        (`retryable=True`).
        """
        return await self._at(identity_id, expected_revision)._revalidate(
            identity_id, actor=actor, reason=reason, request_id=request_id, credential=credential
        )

    async def adopt_session(
        self,
        identity_id: UUID,
        slot: str = "default",
        *,
        actor: str,
        activate: bool = False,
        expected_revision: int | None = None,
    ) -> SessionAdoptionResult:
        """Проверить slot и атомарно принять его как canonical `default`."""
        return await self._at(identity_id, expected_revision)._adopt_session(
            identity_id,
            slot,
            actor=actor,
            activate=activate,
        )

    async def login(self, identity_id: UUID, *, credential: tuple[str, str] | None = None) -> LoginOutcome:
        """Одна попытка входа для identity в `no_session` или `session_invalid`.

        Работает и для `provisioning` — так новый identity логинят до `identities.activate`.
        `CHALLENGED` — платформа ждёт ответа человека: challenge есть в
        `identities.card(id).open_challenge` и в событии `identity.challenge.opened`.

        `credential=(kind, name)` — попросить проверить КОНКРЕТНУЮ запись credentials, а не «первую
        пригодную». Аутентификатор читает её в `ctx.requested_credential` и берёт по имени через
        `ctx.credential(kind, name)`. `None` — прежнее поведение (аутентификатор решает сам).
        """
        return await self._space._run_login(identity_id, credential=credential)

    async def login_many(self, *, service: Service | str | None = None, limit: int = 10) -> LoginReport:
        """Войти в identity без сессии у сервисов с `Authenticator`; одна попытка на identity."""
        return await self._space._run_logins(service=service, limit=limit)

    async def answer_challenge(
        self,
        challenge_id: UUID,
        answer: str,
        *,
        actor: str,
        expected_identity_id: UUID | None = None,
    ) -> LoginOutcome:
        """Ответ человека (код из SMS и т.п.): сохраняется зашифрованным, вход продолжается сразу."""
        return await self._space._answer_challenge(
            challenge_id, answer, actor=actor, expected_identity_id=expected_identity_id
        )
