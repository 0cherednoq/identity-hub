"""Декларация платформы в коде (ТЗ FR-1.2).

`Service` описывает схемы данных, операции и их лимиты, правила операций, коды standing,
верификации и уровни, нормализацию наблюдений, аутентификатор, пробы, классификатор ошибок,
режим аренды и политику прокси. Источник правды — код; `ih_services` — зеркало для админки.
"""

from __future__ import annotations

import inspect
import ipaddress
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Generic, get_args
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

from identity_hub.acceptance import AcceptPolicy
from identity_hub.domain.region_specs import REGIONS
from identity_hub.domain.regions import Normalizer, RegionSpec
from identity_hub.domain.verification import VERIFICATION, VerificationSpec, VerificationTier
from identity_hub.enums import Standing
from identity_hub.errors import ValidationFailed
from identity_hub.protocols import (
    Authenticator,
    AuthProbe,
    ErrorClassifier,
    Fault,
    FileImporter,
    LoginContext,
    StandingProbe,
    TCreds,
    TSession,
    VerificationProbe,
    _FunctionAuthenticator,
    _MappingClassifier,
)

__all__ = [
    "PROBE_OPERATION",
    "EmptyModel",
    "LeaseMode",
    "OperationRule",
    "Pacing",
    "ProxyDownPolicy",
    "ProxyPolicy",
    "Quota",
    "Service",
    "Throttle",
]

PROBE_OPERATION = "probe"
"""Вид операции, который тратят пробы (FR-9.7). Добавляется в сервис автоматически."""

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _normalize_service_url(value: str) -> str:
    """Нормализовать адрес платформы до HTTP(S) origin без userinfo и пути."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("url: нужен непустой HTTP(S) адрес")
    raw = value.strip()
    parsed = urlsplit(raw if "://" in raw else f"https://{raw}")
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or parsed.hostname is None:
        raise ValueError("url: поддерживаются только http и https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("url: логин и пароль запрещены")
    host = parsed.hostname
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if all(label.isdigit() for label in host.split(".")):
            raise ValueError("url: некорректный IP-адрес") from None
        try:
            host = host.encode("idna").decode("ascii").lower()
        except UnicodeError:
            raise ValueError("url: некорректное имя хоста") from None
        labels = host.split(".")
        if not labels or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in labels
        ):
            raise ValueError("url: некорректное имя хоста") from None
    else:
        host = f"[{address.compressed}]" if address.version == 6 else address.compressed
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("url: некорректный порт") from None
    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    suffix = "" if port is None or default_port else f":{port}"
    return f"{scheme}://{host}{suffix}"
_EPOCH = datetime(2000, 1, 3, tzinfo=UTC)  # понедельник: недельные окна начинаются с понедельника


class EmptyModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LeaseMode(StrEnum):
    EXCLUSIVE = "exclusive"
    SHARED = "shared"  # FR-6.6: аренда видна, но не блокирует (почта по IMAP)


class ProxyDownPolicy(StrEnum):
    WAIT = "wait"  # аккаунт недоступен, пока прокси в карантине
    REBIND = "rebind"  # перепривязать к другому прокси пула


@dataclass(frozen=True)
class Quota:
    """Лимит операций за фиксированное окно (FR-7.2).

    `origin` — начало отсчёта окон (для суточных окон — полночь в таймзоне платформы).
    """

    limit: int
    per: timedelta
    origin: datetime = _EPOCH

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("limit квоты должен быть ≥ 1")
        if self.per.total_seconds() < 1 or self.per.total_seconds() != int(self.per.total_seconds()):
            raise ValueError("окно квоты — целое число секунд ≥ 1")
        if self.origin.tzinfo is None:
            raise ValueError("origin квоты должен быть с таймзоной")

    @property
    def window_seconds(self) -> int:
        return int(self.per.total_seconds())

    def window_start(self, now: datetime) -> datetime:
        """Начало окна, как `date_bin(per, now, origin)` в PostgreSQL."""
        elapsed = (now - self.origin).total_seconds()
        return self.origin + timedelta(seconds=(elapsed // self.window_seconds) * self.window_seconds)


@dataclass(frozen=True)
class Throttle:
    """GCRA-ограничение частоты: ``limit`` вызовов за ``per`` с допустимым всплеском."""

    limit: int
    per: timedelta
    burst: int = 1

    def __post_init__(self) -> None:
        if isinstance(self.limit, bool) or self.limit < 1:
            raise ValueError("limit троттлинга должен быть ≥ 1")
        if not isinstance(self.per, timedelta) or self.per.total_seconds() <= 0:
            raise ValueError("per троттлинга должен быть положительным timedelta")
        if isinstance(self.burst, bool) or self.burst < 1:
            raise ValueError("burst троттлинга должен быть ≥ 1")

    @property
    def interval(self) -> timedelta:
        return self.per / self.limit


@dataclass(frozen=True)
class Pacing:
    """Превентивный отдых: после `work.limit` операций за окно — пауза (минуты, случайно из диапазона)."""

    work: Quota
    pause: tuple[float, float]

    def __post_init__(self) -> None:
        lo, hi = self.pause
        if lo < 0 or hi < lo:
            raise ValueError("pause — диапазон минут (min ≤ max, min ≥ 0)")


@dataclass(frozen=True)
class ProxyPolicy:
    """Привязка прокси (FR-8.2, 8.3). `group` — группа прокси, из которой выдаются адреса."""

    group: str = ""
    sticky: bool = True
    max_identities: int | None = None  # None — берётся из прокси (ih_proxies.max_identities)
    on_proxy_down: ProxyDownPolicy = ProxyDownPolicy.WAIT

    def __post_init__(self) -> None:
        if not self.group:
            raise ValueError("ProxyPolicy: нужна группа прокси (group)")


@dataclass(frozen=True)
class OperationRule:
    """Когда операция допустима и сколько её можно (ТЗ §3.9)."""

    standing: frozenset[str] = frozenset({Standing.GOOD, Standing.LIMITED})
    needs_auth: bool = True
    verified: frozenset[str] = frozenset()
    tiers: frozenset[str] = frozenset()
    quotas: tuple[Quota, ...] = ()

    def __post_init__(self) -> None:
        unknown = set(self.standing) - set(Standing)
        if unknown:
            raise ValueError(f"неизвестные значения standing: {sorted(unknown)}")
        windows = [q.window_seconds for q in self.quotas]
        if len(windows) != len(set(windows)):
            raise ValueError("у вида операции не может быть двух квот с одинаковым окном")


@dataclass(frozen=True, eq=False)
class Service(Generic[TCreds, TSession]):  # noqa: UP046 - defaults preserve unparameterized API
    """Декларация платформы (FR-1.2)."""

    name: str
    title: str
    url: str | None = None
    attributes: type[BaseModel] = EmptyModel
    profile: type[BaseModel] = EmptyModel
    credentials: type[TCreds] = field(default=EmptyModel)  # type: ignore[assignment]
    session: type[TSession] = field(default=EmptyModel)  # type: ignore[assignment]
    session_slots: Mapping[str, type[BaseModel]] = field(default_factory=dict)
    session_auth_cookies: frozenset[str] = frozenset()
    filterable_profile: frozenset[str] = frozenset()
    searchable_profile: frozenset[str] = frozenset()
    operations: Mapping[str, OperationRule] = field(default_factory=dict)
    throttles: Mapping[str, Throttle] = field(default_factory=dict)
    standing_codes: Mapping[str, str] = field(default_factory=dict)
    verifications: tuple[VerificationSpec, ...] = ()
    tiers: tuple[VerificationTier, ...] = ()
    normalizers: Mapping[str, Normalizer] = field(default_factory=dict)
    # объект с `async def login(ctx)` или сама функция входа (R2-F27)
    authenticator: (
        Authenticator[TCreds, TSession]
        | Callable[[LoginContext[TCreds, TSession]], Awaitable[TSession]]
        | None
    ) = None
    # предел одного вызова Authenticator.login: зависший вход не держит аккаунт в logging_in
    login_timeout: timedelta = timedelta(seconds=120)
    probes: AuthProbe | StandingProbe | VerificationProbe | None = None  # любое подмножество проб
    probe_timeout: timedelta = timedelta(seconds=60)  # предел одного вызова пробы
    # объект с `classify(exc)` или словарь «тип исключения → Fault»
    error_classifier: ErrorClassifier | Mapping[type[BaseException], Fault] | None = None
    lease_mode: LeaseMode = LeaseMode.EXCLUSIVE
    proxy_policy: ProxyPolicy | None = None
    profile_encrypted: bool = False  # D-003
    importers: tuple[FileImporter, ...] = ()
    quarantine_threshold: float = 5.0  # error_score, при котором availability → quarantined
    # Нормализация external_ref (IMP-08): уникальность и поиск — по результату. Смена функции
    # требует новой версии и перенормализации (`hub.doctor.renormalize`), см. D-149.
    normalize_ref: Callable[[str], str] = str.strip
    normalize_ref_version: int = 0
    # виды credentials кроме `default` (его схема — `credentials`): пароль, TOTP, API-ключи (IMP-09)
    credential_kinds: Mapping[str, type[BaseModel]] = field(default_factory=dict)
    # Default import acceptance policy; a per-import policy takes precedence (D-317).
    default_accept: AcceptPolicy | None = None
    # Отклонённая адаптером пара сразу выключается из ручного выбора (D-319).
    disable_rejected_credentials: bool = False

    def session_schema(self, slot: str) -> type[BaseModel]:
        """Схема конкретного слота; необъявленный слот использует прежнюю общую `session`."""
        if not isinstance(slot, str) or not 1 <= len(slot) <= 64:
            raise ValidationFailed("session slot: от 1 до 64 символов")
        return self.session_slots.get(slot, self.session)

    def session_input(self, slot: str, value: Any) -> Any:
        """Нормализовать удобные входные формы перед валидацией схемой слота."""
        from identity_hub.formats import BrowserSession, Cookies

        schema = self.session_schema(slot)
        if issubclass(schema, BrowserSession) and isinstance(value, Cookies):
            return BrowserSession(cookies=value)
        return value

    def session_expiry(self, model: BaseModel) -> datetime | None:
        """Срок BrowserSession по объявленным auth-cookie; для остальных моделей срока нет."""
        from identity_hub.formats import BrowserSession

        if not self.session_auth_cookies or not isinstance(model, BrowserSession):
            return None
        timestamp = model.expires_at(self.session_auth_cookies)
        return datetime.fromtimestamp(timestamp, UTC) if timestamp is not None else None

    def credential_schema(self, kind: str) -> type[BaseModel]:
        """Схема секрета вида credentials; неизвестный вид — `ValidationFailed` (D-214)."""
        if kind == "default":
            return self.credentials
        schema = self.credential_kinds.get(kind)
        if schema is None:
            raise ValidationFailed(f"у сервиса {self.name} нет вида credentials {kind!r}")
        return schema

    def credential_key(self, kind: str, name: str) -> tuple[str, str]:
        """Проверенные (kind, name): вид объявлен, имя — от 1 до 128 символов."""
        self.credential_schema(kind)
        if not isinstance(name, str) or not 1 <= len(name) <= 128:
            raise ValidationFailed("имя credentials: от 1 до 128 символов")
        return kind, name

    def __post_init__(self) -> None:
        if self.url is not None:
            object.__setattr__(self, "url", _normalize_service_url(self.url))
        errors = self._validate()
        if not errors:
            # функциональные формы адаптеров приводятся к объектам протоколов (R2-F27)
            if self.authenticator is not None and not hasattr(self.authenticator, "login"):
                object.__setattr__(self, "authenticator", _FunctionAuthenticator(self.authenticator))
            if isinstance(self.error_classifier, Mapping):
                object.__setattr__(self, "error_classifier", _MappingClassifier(self.error_classifier))
        if errors:
            raise ValidationFailed(f"сервис {self.name!r} объявлен некорректно", errors)
        if PROBE_OPERATION not in self.operations:
            ops = {
                **self.operations,
                PROBE_OPERATION: OperationRule(needs_auth=False, standing=frozenset(Standing)),
            }
            object.__setattr__(self, "operations", ops)

    # --------------------------------------------------------- доступ

    def normalize_external_ref(self, external_ref: str) -> str:
        """Нормализованный ref для уникальности и поиска; некорректный ввод — `ValidationFailed`.

        В сообщение об ошибке не попадает исходное значение (это может быть телефон или email).
        """
        if not isinstance(external_ref, str) or not external_ref or len(external_ref) > 256:
            raise ValidationFailed("external_ref: от 1 до 256 символов")
        try:
            normalized = self.normalize_ref(external_ref)
        except (ValueError, TypeError) as exc:
            raise ValidationFailed(
                f"external_ref не прошёл нормализацию сервиса {self.name}: {type(exc).__name__}"
            ) from None
        if not isinstance(normalized, str) or not normalized or len(normalized) > 256:
            raise ValidationFailed(
                f"нормализация external_ref сервиса {self.name} вернула пустое или длинное значение"
            )
        return normalized

    def rule(self, kind: str) -> OperationRule:
        try:
            return self.operations[kind]
        except KeyError:
            raise ValidationFailed(f"сервис {self.name}: неизвестная операция {kind!r}") from None

    def verification(self, factor: str) -> VerificationSpec:
        for spec in self.verifications:
            if spec.name == factor:
                return spec
        raise ValidationFailed(f"сервис {self.name}: неизвестная верификация {factor!r}")

    @property
    def factor_names(self) -> frozenset[str]:
        return frozenset(v.name for v in self.verifications)

    @property
    def tiers_by_name(self) -> dict[str, VerificationTier]:
        return {t.name: t for t in self.tiers}

    def region_spec(self, region: str) -> RegionSpec:
        """Спецификация региона с нормализацией сервиса (FR-9.5)."""
        base = VERIFICATION if region == VERIFICATION.name else REGIONS.get(region)
        if base is None:
            raise ValidationFailed(f"неизвестный регион {region!r}")
        normalizer = self.normalizers.get(region)
        if normalizer is None:
            return base
        if base.normalize is None:
            return replace(base, normalize=normalizer)
        inner = base.normalize

        def chained(current: str, observed: str) -> str:
            return normalizer(current, inner(current, observed))

        return replace(base, normalize=chained)

    def standing_for_code(self, code: str) -> str | None:
        """Код платформы → состояние standing; None — код неизвестен (FR-9.6)."""
        return self.standing_codes.get(code)

    def registry_row(self) -> dict[str, Any]:
        return {"name": self.name, "title": self.title, "url": self.url}

    # --------------------------------------------------------- валидация

    def _validate(self) -> list[str]:
        errors: list[str] = []
        if not _NAME.match(self.name):
            errors.append("name: латиница в нижнем регистре, цифры и _, до 64 символов")
        if not callable(self.normalize_ref):
            errors.append("normalize_ref: нужна функция str -> str")
        if self.normalize_ref_version < 0:
            errors.append("normalize_ref_version ≥ 0")
        if self.normalize_ref is not str.strip and self.normalize_ref_version == 0:
            errors.append(
                "normalize_ref_version: своя нормализация требует версии ≥ 1 (0 — str.strip по умолчанию)"
            )
        unknown_filterable = {
            path for path in self.filterable_profile if not _model_path_exists(self.profile, path)
        }
        if unknown_filterable:
            errors.append(f"filterable_profile: нет в схеме profile: {sorted(unknown_filterable)}")
        if self.profile_encrypted and any("." in path for path in self.filterable_profile):
            errors.append("filterable_profile: вложенные пути несовместимы с profile_encrypted")
        profile_fields = set(self.profile.model_fields)
        unknown_searchable = set(self.searchable_profile) - profile_fields
        if unknown_searchable:
            errors.append(f"searchable_profile: нет в схеме profile: {sorted(unknown_searchable)}")
        if any(not isinstance(name, str) or not name for name in self.session_auth_cookies):
            errors.append("session_auth_cookies: имена cookie должны быть непустыми строками")
        if self.default_accept is not None and not isinstance(self.default_accept, AcceptPolicy):
            errors.append("default_accept: нужен AcceptPolicy")
        if not isinstance(self.disable_rejected_credentials, bool):
            errors.append("disable_rejected_credentials: нужен bool")
        for code, state in self.standing_codes.items():
            if state not in set(Standing):
                errors.append(f"standing_codes[{code!r}]: неизвестное состояние {state!r}")
        factors = [v.name for v in self.verifications]
        if len(factors) != len(set(factors)):
            errors.append("verifications: повторяющиеся имена факторов")
        known = set(factors)
        for spec in self.verifications:
            missing = spec.requires - known
            if missing:
                errors.append(f"verification {spec.name}: неизвестные зависимости {sorted(missing)}")
        errors.extend(_cycle_errors(self.verifications))
        tier_names = {t.name for t in self.tiers}
        for tier in self.tiers:
            missing = tier.factors - known
            if missing:
                errors.append(f"tier {tier.name}: неизвестные факторы {sorted(missing)}")
        for kind, rule in self.operations.items():
            if not _NAME.match(kind):
                errors.append(f"operation {kind!r}: некорректное имя")
            if rule.verified - known:
                errors.append(f"operation {kind}: неизвестные факторы {sorted(rule.verified - known)}")
            if rule.tiers - tier_names:
                errors.append(f"operation {kind}: неизвестные уровни {sorted(rule.tiers - tier_names)}")
        for name, throttle in self.throttles.items():
            if not _NAME.match(name):
                errors.append(f"throttles[{name!r}]: некорректное имя")
            if not isinstance(throttle, Throttle):
                errors.append(f"throttles[{name!r}]: нужен Throttle")
        for slot, schema in self.session_slots.items():
            if not isinstance(slot, str) or not 1 <= len(slot) <= 64:
                errors.append("session_slots: имя слота — от 1 до 64 символов")
            if not isinstance(schema, type) or not issubclass(schema, BaseModel):
                errors.append(f"session_slots[{slot!r}]: нужна Pydantic-модель")
        for region in self.normalizers:
            if region not in REGIONS and region != VERIFICATION.name:
                errors.append(f"normalizers: неизвестный регион {region!r}")
            elif region in REGIONS and not REGIONS[region].synced:
                errors.append(f"normalizers: регион {region!r} не синхронизируется")
        errors.extend(self._adapter_errors())
        return errors

    def _adapter_errors(self) -> list[str]:
        """Адаптеры проверяются при объявлении: ошибка сразу, а не на первом входе в проде (R2-F27)."""
        errors: list[str] = []
        auth = self.authenticator
        if auth is not None:
            login = getattr(auth, "login", None)
            if login is None and not callable(auth):
                errors.append(
                    "authenticator: нужен объект с методом `async def login(self, ctx: LoginContext) -> "
                    "BaseModel` или функция `async def login(ctx)`"
                )
            elif not _is_async(login if login is not None else auth):
                errors.append("authenticator.login: нужна корутина — `async def login(...)`")
        if self.probes is not None:
            present = [name for name in _PROBES if getattr(self.probes, name, None) is not None]
            if not present:
                errors.append(
                    f"probes ({type(self.probes).__name__}): нужен хотя бы один из методов "
                    "`async def auth(ctx)`, `async def standing(ctx)`, `async def verification(ctx, factor)`"
                )
            for name in present:
                if not _is_async(getattr(self.probes, name)):
                    errors.append(
                        f"probes.{name} ({type(self.probes).__name__}): нужен `async def {name}(...)`"
                    )
        classifier = self.error_classifier
        if (
            classifier is not None
            and not isinstance(classifier, Mapping)
            and not callable(getattr(classifier, "classify", None))
        ):
            errors.append(
                "error_classifier: нужен объект с методом `classify(exc) -> Fault | None` "
                "или словарь {тип исключения: Fault}"
            )
        for index, importer in enumerate(self.importers):
            label = f"importers[{index}] ({type(importer).__name__})"
            if not callable(getattr(importer, "can_import", None)):
                errors.append(f"{label}: нужен метод `can_import(path) -> bool`")
            if not callable(getattr(importer, "read", None)):
                errors.append(
                    f"{label}: нужен метод `read(path)` — async-генератор с `yield ImportRecord` "
                    "или функция, возвращающая список записей"
                )
        return errors


_PROBES = ("auth", "standing", "verification")


def _model_path_exists(model: type[BaseModel], path: str) -> bool:
    if not isinstance(path, str) or not path or any(not part for part in path.split(".")):
        return False
    current = model
    parts = path.split(".")
    for index, part in enumerate(parts):
        field_info = current.model_fields.get(part)
        if field_info is None:
            return False
        if index == len(parts) - 1:
            return True
        nested = _nested_model(field_info.annotation)
        if nested is None:
            return False
        current = nested
    return True


def _nested_model(annotation: Any) -> type[BaseModel] | None:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for argument in get_args(annotation):
        nested = _nested_model(argument)
        if nested is not None:
            return nested
    return None


def _is_async(function: object) -> bool:
    """Корутинная функция или объект с асинхронным `__call__`."""
    return inspect.iscoroutinefunction(function) or inspect.iscoroutinefunction(
        getattr(function, "__call__", None)  # noqa: B004 - объект с async __call__
    )


def _cycle_errors(specs: tuple[VerificationSpec, ...]) -> list[str]:
    graph = {s.name: s.requires for s in specs}
    visiting: set[str] = set()
    done: set[str] = set()
    errors: list[str] = []

    def visit(node: str) -> None:
        if node in done or node not in graph:
            return
        if node in visiting:
            errors.append(f"verifications: циклическая зависимость через {node!r}")
            return
        visiting.add(node)
        for dep in graph[node]:
            visit(dep)
        visiting.discard(node)
        done.add(node)

    for name in graph:
        visit(name)
    return errors
