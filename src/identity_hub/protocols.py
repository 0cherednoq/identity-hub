"""Точки расширения, которые реализует пользователь (ТЗ §5.3).

Протоколы используются только на публичных границах; внутри библиотеки — конкретные классы.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel

from identity_hub.errors import ValidationFailed

if TYPE_CHECKING:
    from identity_hub.importing import ImportRecord

__all__ = [
    "Authenticator",
    "Blame",
    "ErrorClassifier",
    "EventConsumer",
    "Fault",
    "FileImporter",
    "LinkedIdentity",
    "LoginChallenge",
    "LoginContext",
    "ProbeContext",
    "ProbeResult",
    "Probes",
    "SecretsProvider",
]


# ---------------------------------------------------------------- вход


class LinkedIdentity(Protocol):
    """Связанный аккаунт, взятый библиотекой в аренду для входа (FR-4.3)."""

    @property
    def identity_id(self) -> UUID: ...

    @property
    def external_ref(self) -> str: ...

    async def credentials(self) -> BaseModel | None: ...

    async def session(self, slot: str = "default") -> BaseModel | None: ...


class LoginContext(Protocol):
    """Контекст входа, передаётся `Authenticator.login`."""

    @property
    def identity_id(self) -> UUID: ...

    @property
    def external_ref(self) -> str: ...

    @property
    def proxy_url(self) -> str | None: ...

    @property
    def challenge_answer(self) -> str | None:
        """Ответ человека на открытый challenge (при повторном вызове после `answer_challenge`)."""
        ...

    @property
    def challenge_context(self) -> BaseModel | Mapping[str, Any] | None:
        """Контекст, сохранённый при `LoginChallenge` (phone_code_hash и т.п.)."""
        ...

    async def credentials(self) -> BaseModel | None: ...

    async def linked(self, role: str) -> LinkedIdentity | None: ...


class LoginChallenge(Exception):
    """Платформа требует ответ человека (FR-5.5). Бросается из `Authenticator.login`."""

    def __init__(
        self,
        kind: str,
        hint: str,
        context: BaseModel | Mapping[str, Any] | None = None,
        expires_in: timedelta | None = None,
    ) -> None:
        self.kind = kind
        self.hint = hint
        self.context = context
        self.expires_in = expires_in
        super().__init__(f"challenge {kind}: {hint}")


@runtime_checkable
class Authenticator(Protocol):
    async def login(self, ctx: LoginContext) -> BaseModel:
        """Выполнить вход и вернуть сессию (схема `Service.session`) или бросить `LoginChallenge`."""
        ...

    async def refresh(self, credentials: BaseModel | None, session: BaseModel) -> BaseModel | None:
        """Обновить сессию; None — обновление невозможно, нужен вход."""
        ...


# ---------------------------------------------------------------- пробы


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """Ответ пробы. `state=None` — статус не распознан (FR-9.6); `code` — код платформы."""

    state: str | None
    raw_status: str | None = None
    code: str | None = None
    until: datetime | None = None
    observed_at: datetime | None = None  # None — время обработки

    def __post_init__(self) -> None:
        # наивное время дало бы TypeError при сравнении со временем БД в машине регионов
        for name in ("until", "observed_at"):
            value = getattr(self, name)
            if value is not None and value.tzinfo is None:
                raise ValidationFailed(f"ProbeResult.{name} должен быть с таймзоной (например, UTC)")


class ProbeContext(Protocol):
    @property
    def identity_id(self) -> UUID: ...

    @property
    def proxy_url(self) -> str | None: ...

    async def credentials(self) -> BaseModel | None: ...

    async def session(self, slot: str = "default") -> BaseModel | None: ...


class Probes(Protocol):
    """Активные наблюдения по регионам (FR-9.7). Необъявленная проба — `None` (регион не проверяется)."""

    async def auth(self, ctx: ProbeContext) -> ProbeResult | None: ...

    async def standing(self, ctx: ProbeContext) -> ProbeResult | None: ...

    async def verification(self, ctx: ProbeContext, factor: str) -> ProbeResult | None: ...


# ---------------------------------------------------------------- классификатор ошибок


class Blame(StrEnum):
    """Чья вина в ошибке операции (FR-7.3)."""

    IDENTITY = "identity"  # аккаунт: растёт error_score, возможен карантин / кулдаун
    PROXY = "proxy"  # прокси: здоровье прокси
    REQUEST = "request"  # запрос пользователя: состояние не меняется
    SESSION_INVALID = "session_invalid"  # auth → session_invalid
    IDENTITY_BANNED = "identity_banned"  # наблюдение standing


@dataclass(frozen=True, slots=True)
class Fault:
    """Результат классификации исключения."""

    blame: Blame
    retry_after: timedelta | None = None  # rate limit / flood-wait → availability cooling
    standing: str | None = None  # целевое standing для IDENTITY_BANNED (по умолчанию banned)
    code: str | None = None  # код платформы
    raw_status: str | None = None
    weight: float = 1.0  # вклад в error_score


class ErrorClassifier(Protocol):
    def classify(self, exc: BaseException) -> Fault | None:
        """None — неизвестная ошибка: считается виной запроса."""
        ...


# ---------------------------------------------------------------- секреты, события, импорт


class SecretsProvider(Protocol):
    """Источник мастер-ключей (S-4). Ключи — 32 байта (AES-256)."""

    def keys(self) -> Mapping[str, bytes]:
        """Все известные KEK по key_id (включая старые — для расшифровки после ротации)."""
        ...

    def active_key_id(self) -> str:
        """KEK, которым шифруются новые записи."""
        ...


class EventConsumer(Protocol):
    """Python-колбэк, получающий события после коммита (FR-13.5). At-least-once."""

    async def __call__(self, event: Any) -> None: ...


class FileImporter(Protocol):
    """Импортёр файлов сервиса (.session, tdata, JSON с cookies) → ImportRecord (FR-2.6)."""

    def can_import(self, path: Path) -> bool: ...

    def read(self, path: Path) -> AsyncIterator[ImportRecord]: ...
