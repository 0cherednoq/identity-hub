"""Декларация платформы в коде (ТЗ FR-1.2).

`Service` описывает схемы данных, операции и их лимиты, правила операций, коды standing,
верификации и уровни, нормализацию наблюдений, аутентификатор, пробы, классификатор ошибок,
режим аренды и политику прокси. Источник правды — код; `ih_services` — зеркало для админки.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

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
class Service:
    """Декларация платформы (FR-1.2)."""

    name: str
    title: str
    attributes: type[BaseModel] = EmptyModel
    profile: type[BaseModel] = EmptyModel
    credentials: type[BaseModel] = EmptyModel
    session: type[BaseModel] = EmptyModel
    session_slots: Mapping[str, type[BaseModel]] = field(default_factory=dict)
    filterable_profile: frozenset[str] = frozenset()
    searchable_profile: frozenset[str] = frozenset()
    operations: Mapping[str, OperationRule] = field(default_factory=dict)
    throttles: Mapping[str, Throttle] = field(default_factory=dict)
    standing_codes: Mapping[str, str] = field(default_factory=dict)
    verifications: tuple[VerificationSpec, ...] = ()
    tiers: tuple[VerificationTier, ...] = ()
    normalizers: Mapping[str, Normalizer] = field(default_factory=dict)
    # объект с `async def login(ctx)` или сама функция входа (R2-F27)
    authenticator: Authenticator | Callable[[LoginContext], Awaitable[BaseModel]] | None = None
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

    def session_schema(self, slot: str) -> type[BaseModel]:
        """Схема конкретного слота; необъявленный слот использует прежнюю общую `session`."""
        if not isinstance(slot, str) or not 1 <= len(slot) <= 64:
            raise ValidationFailed("session slot: от 1 до 64 символов")
        return self.session_slots.get(slot, self.session)

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
        return {"name": self.name, "title": self.title}

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
        profile_fields = set(self.profile.model_fields)
        for attr in ("filterable_profile", "searchable_profile"):
            unknown = set(getattr(self, attr)) - profile_fields
            if unknown:
                errors.append(f"{attr}: нет в схеме profile: {sorted(unknown)}")
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
