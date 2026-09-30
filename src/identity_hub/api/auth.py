"""`space.auth` — явный вход через `Authenticator` сервиса и ответ на challenge (IMP-23).

Внутри обычной аренды библиотека не логинит: вход запускается только этими методами.
"""

from __future__ import annotations

from uuid import UUID

from identity_hub.api._base import Namespace
from identity_hub.auth import LoginOutcome, LoginReport
from identity_hub.service import Service

__all__ = ["Auth"]


class Auth(Namespace):
    """Вход identity: `space.auth`."""

    __slots__ = ()

    async def login(self, identity_id: UUID) -> LoginOutcome:
        """Одна попытка входа для identity в `no_session` или `session_invalid`.

        Работает и для `provisioning` — так новый identity логинят до `identities.activate`.
        `CHALLENGED` — платформа ждёт ответа человека: challenge есть в
        `identities.card(id).open_challenge` и в событии `identity.challenge.opened`.
        """
        return await self._space._run_login(identity_id)

    async def login_many(self, *, service: Service | str | None = None, limit: int = 10) -> LoginReport:
        """Войти в identity без сессии у сервисов с `Authenticator`; одна попытка на identity."""
        return await self._space._run_logins(service=service, limit=limit)

    async def answer_challenge(
        self,
        challenge_id: UUID,
        answer: str,
        *,
        actor: str = "admin",
        expected_identity_id: UUID | None = None,
    ) -> LoginOutcome:
        """Ответ человека (код из SMS и т.п.): сохраняется зашифрованным, вход продолжается сразу."""
        return await self._space._answer_challenge(
            challenge_id, answer, actor=actor, expected_identity_id=expected_identity_id
        )
