"""Точки расширения, которые реализует пользователь (ТЗ §5.3).

Протоколы используются только на публичных границах; внутри библиотеки — конкретные классы.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
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
    "AuthProbe",
    "Authenticator",
    "Blame",
    "ErrorClassifier",
    "Fault",
    "FileImporter",
    "LinkedIdentity",
    "LoginChallenge",
    "LoginContext",
    "ProbeContext",
    "ProbeResult",
    "Probes",
    "SecretsProvider",
    "SessionRefresher",
    "StandingProbe",
    "VerificationProbe",
]


# ---------------------------------------------------------------- вход


class LinkedIdentity(Protocol):
    """Связанный аккаунт, взятый библиотекой в аренду для входа (FR-4.3)."""

    @property
    def identity_id(self) -> UUID: ...

    @property
    def external_ref(self) -> str: ...

    async def credentials(self) -> BaseModel | None: ...

    async def credential(self, kind: str = "default", name: str = "default") -> BaseModel | None:
        """Запись credentials по виду и имени; выключенная, отозванная или истёкшая — `None` (D-216)."""
        ...

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

    async def credential(self, kind: str = "default", name: str = "default") -> BaseModel | None:
        """Запись credentials по виду и имени; выключенная, отозванная или истёкшая — `None` (D-216)."""
        ...

    async def session(self, slot: str = "default") -> BaseModel | None:
        """Последняя сохранённая сессия слота, в том числе недействительная (повторный вход), или None."""
        ...

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
    """Вход на платформе. Вместо объекта можно передать функцию `async def login(ctx)` (R2-F27)."""

    async def login(self, ctx: LoginContext) -> BaseModel:
        """Выполнить вход и вернуть сессию по схеме `Service.session`; вопрос человеку — `LoginChallenge`."""
        ...


@runtime_checkable
class SessionRefresher(Protocol):
    """Необязательное обновление сессии без полного входа. Библиотека пока его не вызывает."""

    async def refresh(self, credentials: BaseModel | None, session: BaseModel) -> BaseModel | None: ...


class _FunctionAuthenticator:
    """`Service(authenticator=login_fn)`: функция входа в форме объекта с методом `login`."""

    __slots__ = ("_login",)

    def __init__(self, login: Callable[[LoginContext], Awaitable[BaseModel]]) -> None:
        self._login = login

    async def login(self, ctx: LoginContext) -> BaseModel:
        return await self._login(ctx)

    def __repr__(self) -> str:
        return f"Authenticator({getattr(self._login, '__qualname__', self._login)!r})"


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
    """Контекст пробы, передаётся `auth`/`standing`/`verification`."""

    @property
    def identity_id(self) -> UUID: ...

    @property
    def external_ref(self) -> str: ...

    @property
    def proxy_url(self) -> str | None: ...

    async def credentials(self) -> BaseModel | None: ...

    async def credential(self, kind: str = "default", name: str = "default") -> BaseModel | None:
        """Запись credentials по виду и имени; выключенная, отозванная или истёкшая — `None` (D-216)."""
        ...

    async def session(self, slot: str = "default") -> BaseModel | None: ...


class AuthProbe(Protocol):
    async def auth(self, ctx: ProbeContext) -> ProbeResult | None: ...


class StandingProbe(Protocol):
    async def standing(self, ctx: ProbeContext) -> ProbeResult | None: ...


class VerificationProbe(Protocol):
    async def verification(self, ctx: ProbeContext, factor: str) -> ProbeResult | None: ...


class Probes(AuthProbe, StandingProbe, VerificationProbe, Protocol):
    """Все три пробы. `Service(probes=...)` принимает объект с любым их подмножеством (R2-F27):
    регион без пробы не сверяется. Каждая проба — `async def`; None — «не знаю»."""


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
    """Классификатор ошибок операций. Если `classify` принимает именованный `kind`, библиотека передаёт
    вид операции. Вместо объекта можно передать словарь `{тип исключения: Fault}` (R2-F27)."""

    def classify(self, exc: BaseException) -> Fault | None:
        """None — неизвестная ошибка: считается виной запроса."""
        ...


class _MappingClassifier:
    """`Service(error_classifier={TimeoutError: Fault(...)})`: тип исключения или его предок → `Fault`."""

    __slots__ = ("_faults",)

    def __init__(self, faults: Mapping[type[BaseException], Fault]) -> None:
        self._faults = dict(faults)

    def classify(self, exc: BaseException, *, kind: str | None = None) -> Fault | None:
        for cls in type(exc).__mro__:
            if cls in self._faults:
                return self._faults[cls]
        return None


# ---------------------------------------------------------------- секреты, события, импорт


class SecretsProvider(Protocol):
    """Источник мастер-ключей (S-4). Ключи — 32 байта (AES-256)."""

    def keys(self) -> Mapping[str, bytes]:
        """Все известные KEK по key_id (включая старые — для расшифровки после ротации)."""
        ...

    def active_key_id(self) -> str:
        """KEK, которым шифруются новые записи."""
        ...


class FileImporter(Protocol):
    """Импортёр файлов сервиса (.session, tdata, JSON с cookies) → ImportRecord (FR-2.6).

    `read` — async-генератор, обычная функция со списком (любым `Iterable`) или `async def`,
    возвращающая список (R2-F27).
    """

    def can_import(self, path: Path) -> bool: ...

    def read(
        self, path: Path
    ) -> AsyncIterator[ImportRecord] | Iterable[ImportRecord] | Awaitable[Iterable[ImportRecord]]: ...
