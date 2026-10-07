"""`space.identities` — чтение, создание и изменение identity (IMP-23)."""

from __future__ import annotations

import builtins
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import BaseModel

from identity_hub.api._base import Namespace
from identity_hub.domain.credentials import CredentialInput
from identity_hub.domain.query import SearchQuery, parse_query
from identity_hub.domain.regions import RegionDecision
from identity_hub.enums import AuthState, Availability, ChangeSource, Lifecycle, Standing
from identity_hub.errors import AlreadyExists, NotFound, ValidationFailed
from identity_hub.identities import UNSET, PreparedIdentity, _Unset, prepare_identity
from identity_hub.service import Service
from identity_hub.state import DEFAULT_SLOT, Observation, Override
from identity_hub.store.base import IdentityRow, StoreTx

if TYPE_CHECKING:
    from identity_hub.read.cards import IdentityCard

__all__ = [
    "CreateManyResult",
    "Identities",
    "Identity",
    "IdentityCreate",
    "IdentityFilter",
    "IdentityRef",
    "OnConflict",
    "Page",
    "UpdateManyResult",
]

# Счётчик `count()` точный до этого значения; дальше возвращается `COUNT_CAP + 1`.
COUNT_CAP = 1_000_000
# Предел `create_many`: одна транзакция; большие объёмы — `space.imports`.
CREATE_MANY_LIMIT = 1000

type SchemaInput = BaseModel | Mapping[str, Any]


class OnConflict(StrEnum):
    """Что делает `create_many`, если identity с таким id или ref уже есть в пространстве."""

    FAIL = "fail"
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class IdentityRef:
    """Ссылка на identity: id и пара `(service, external_ref)`."""

    id: UUID
    service: str
    external_ref: str


@dataclass(frozen=True, slots=True)
class Identity:
    """Identity (аккаунт во внешнем сервисе) без секретов.

    `profile` — открытая часть профиля: при `Service.profile_encrypted` только
    `filterable_profile`. `revision` растёт с каждым событием identity.
    """

    id: UUID
    service: str
    external_ref: str
    normalized_ref: str  # ключ уникальности: Service.normalize_ref(external_ref)
    lifecycle: Lifecycle
    standing: Standing
    standing_code: str | None
    standing_until: datetime | None
    auth: AuthState
    availability: Availability
    availability_until: datetime | None
    verified_factors: frozenset[str]
    tags: frozenset[str]
    labels: Mapping[str, Any]
    profile: Mapping[str, Any]
    attributes: Mapping[str, Any]
    note: str | None
    error_score: float
    revision: int
    created_at: datetime
    updated_at: datetime

    @property
    def ref(self) -> IdentityRef:
        return IdentityRef(self.id, self.service, self.external_ref)

    @classmethod
    def from_row(cls, row: IdentityRow) -> Identity:
        return cls(
            id=row.id,
            service=row.service,
            external_ref=row.external_ref,
            normalized_ref=row.normalized_ref or row.external_ref.strip(),
            lifecycle=Lifecycle(row.lifecycle),
            standing=Standing(row.standing),
            standing_code=row.standing_code,
            standing_until=row.standing_until,
            auth=AuthState(row.auth_state),
            availability=Availability(row.availability),
            availability_until=row.availability_until,
            verified_factors=frozenset(row.verified_factors),
            tags=frozenset(row.tags),
            labels=dict(row.labels),
            profile=dict(row.profile),
            attributes=dict(row.attributes),
            note=row.note,
            error_score=row.error_score,
            revision=row.revision,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


@dataclass(frozen=True, slots=True)
class Page[T]:
    """Страница результата. `next_cursor` передаётся в `after` следующего вызова.

    `total` — только при `count=True`: точное число до `COUNT_LIMIT` (1000), больше —
    `COUNT_LIMIT + 1` («больше 1000»). Точное число до `COUNT_CAP` — `identities.count()` (R1-28).
    """

    items: list[T]
    next_cursor: str | None
    total: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class IdentityCreate:
    """Данные новой identity для `create`, `ensure` и `create_many`.

    Секреты и профиль проверяются схемами сервиса и шифруются до транзакции. `session` —
    сокращение для слота `"default"`; вместе с `sessions["default"]` передавать нельзя.
    `links` — роль → id существующей identity (в `create_many` — и создаваемой в той же пачке).
    """

    service: str | Service
    external_ref: str
    id: UUID | None = None
    credentials: SchemaInput | None = field(default=None, repr=False)
    session: SchemaInput | None = field(default=None, repr=False)
    sessions: Mapping[str, SchemaInput] = field(default_factory=dict, repr=False)
    attributes: SchemaInput | None = None
    profile: SchemaInput | None = field(default=None, repr=False)
    labels: Mapping[str, Any] = field(default_factory=dict)
    tags: Collection[str] = ()
    note: str | None = None
    links: Mapping[str, UUID] = field(default_factory=dict)
    lifecycle: Lifecycle | str = Lifecycle.PROVISIONING
    auth: AuthState | str | None = None  # None — по наличию сессии
    # записи credentials кроме default: API-ключи, TOTP и т.п. (IMP-09, D-217)
    credential_records: Sequence[CredentialInput] = field(default=(), repr=False)

    def __repr__(self) -> str:  # секреты не попадают в repr
        return f"IdentityCreate(service={self.service_name!r}, external_ref={self.external_ref!r})"

    @property
    def service_name(self) -> str:
        return self.service.name if isinstance(self.service, Service) else self.service


@dataclass(frozen=True, slots=True)
class CreateManyResult:
    """Итог `create_many`: созданные (в порядке входа) и пропущенные существующие."""

    created: list[Identity]
    skipped: list[IdentityRef]


@dataclass(frozen=True, slots=True)
class UpdateManyResult:
    """Итог атомарного массового изменения identity."""

    changed: int
    skipped: int


def _values(value: str | Collection[str] | None) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, str):
        return frozenset({value})
    return frozenset(str(v) for v in value)


@dataclass(frozen=True, slots=True)
class IdentityFilter:
    """Структурированный фильтр `identities.list()`/`count()`.

    Внутри одного поля со списком значений — ИЛИ, между полями — И. `tags` требует все теги.
    `profile` — только поля `filterable_profile` выбранных сервисов.
    """

    service: str | Service | Collection[str | Service] | None = None
    lifecycle: str | Collection[str] | None = None
    standing: str | Collection[str] | None = None
    auth: str | Collection[str] | None = None
    availability: str | Collection[str] | None = None
    tags: Collection[str] = ()
    labels: Mapping[str, Any] = field(default_factory=dict)
    profile: Mapping[str, Any] = field(default_factory=dict)

    def services(self) -> frozenset[str]:
        raw = self.service
        if raw is None:
            return frozenset()
        items = [raw] if isinstance(raw, str | Service) else list(raw)
        return frozenset(s.name if isinstance(s, Service) else s for s in items)

    def to_query(self, filterable: frozenset[str]) -> SearchQuery:
        regions = {
            name: values
            for name, values in (
                ("lifecycle", _values(self.lifecycle)),
                ("standing", _values(self.standing)),
                ("auth", _values(self.auth)),
                ("availability", _values(self.availability)),
            )
            if values
        }
        unknown = set(self.profile) - filterable
        if unknown:
            raise ValidationFailed(f"поля профиля не в filterable_profile: {', '.join(sorted(unknown))}")
        clash = set(self.labels) & filterable
        if clash:
            raise ValidationFailed(
                f"метки совпадают с filterable_profile, фильтр неоднозначен: {', '.join(sorted(clash))}"
            )
        filters = {key: frozenset({str(value)}) for key, value in {**self.labels, **self.profile}.items()}
        return SearchQuery(
            services=self.services(),
            regions=regions,
            tags=frozenset(self.tags),
            filters=filters,
        )


type _Prepared = list[tuple[Service, PreparedIdentity]]


class Identities(Namespace):
    """Identity пространства: `space.identities`."""

    __slots__ = ()

    # ------------------------------------------------------------------ чтение

    async def get(self, identity_id: UUID) -> Identity:
        """Identity этого пространства; нет или в другом пространстве — `NotFound`."""
        identity = await self.find(identity_id)
        if identity is None:
            raise NotFound("identity", identity_id)
        return identity

    async def find(
        self,
        identity_id: UUID | None = None,
        *,
        service: str | Service | None = None,
        external_ref: str | None = None,
    ) -> Identity | None:
        """Identity по id или по паре `(service, external_ref)`; нет в этом пространстве — `None`."""
        by_ref = service is not None or external_ref is not None
        if (identity_id is None) == (not by_ref) or (by_ref and (service is None or external_ref is None)):
            raise ValidationFailed("укажите identity_id или пару service и external_ref")
        async with self._space._tx(own=True) as tx:
            if identity_id is None:
                assert service is not None and external_ref is not None
                svc = self._space.hub.service(service)
                key = (svc.name, svc.normalize_external_ref(external_ref))
                found = (await tx.find_by_refs([key])).get(key)
                if found is None or found[1] != tx.space.id:
                    return None
                identity_id = found[0]
            row = await tx.get_identity(identity_id)
        return None if row is None else Identity.from_row(row)

    async def get_by_ref(self, service: str | Service, external_ref: str) -> Identity:
        """Identity по `(service, external_ref)` или `NotFound`."""
        identity = await self.find(service=service, external_ref=external_ref)
        if identity is None:
            name = service.name if isinstance(service, Service) else service
            raise NotFound("identity", f"{name}:{external_ref}")
        return identity

    async def card(self, identity_id: UUID) -> IdentityCard:
        """Полная карточка: регионы, верификации, аренда, прокси, квоты, `eligible`, `blockers`."""
        return await self._space._identity_card(identity_id)

    async def search(
        self,
        query: str | SearchQuery = "",
        *,
        limit: int = 50,
        after: str | None = None,
        count: bool = False,
    ) -> Page[Identity]:
        """Поиск по строке запроса (синтаксис — руководство, раздел «Поиск»)."""
        return await self._page(parse_query(query) if isinstance(query, str) else query, limit, after, count)

    async def list(
        self,
        where: IdentityFilter | None = None,
        *,
        limit: int = 50,
        after: str | None = None,
        count: bool = False,
    ) -> Page[Identity]:
        """Страница по структурированному фильтру; порядок — от новых к старым."""
        return await self._page(self._query(where), limit, after, count)

    async def count(self, where: IdentityFilter | None = None) -> int:
        """Число identity по фильтру: точное до `COUNT_CAP` (1 000 000), больше — `COUNT_CAP + 1`."""
        flt = self._space._search_filter(self._query(where))
        async with self._space._tx(own=True) as tx:
            _rows, total = await tx.search_identities(flt, limit=1, after=None, count_limit=COUNT_CAP)
        assert total is not None
        return total

    # ------------------------------------------------------------------ создание

    async def create(self, spec: IdentityCreate, *, actor: str) -> Identity:
        """Создать identity; если такой id или `(service, external_ref)` уже есть — `AlreadyExists`."""
        svc, prepared = self._prepare(spec)
        async with self._space._tx() as tx:
            await self._space._create_one(tx, svc, prepared, actor=actor, fail_existing=True)
            return await self._read(tx, prepared.row.id)

    async def ensure(self, spec: IdentityCreate, *, actor: str) -> tuple[Identity, bool]:
        """Вернуть существующую identity без изменений (`False`) или создать её (`True`).

        Identity с тем же ref в другом пространстве — `AlreadyExists`.
        """
        svc, prepared = self._prepare(spec)
        async with self._space._tx() as tx:
            result = await self._space._create_one(tx, svc, prepared, actor=actor)
            return await self._read(tx, result.identity_id), result.created

    async def create_many(
        self,
        specs: Iterable[IdentityCreate],
        *,
        on_conflict: OnConflict | str = OnConflict.FAIL,
        actor: str,
    ) -> CreateManyResult:
        """Атомарно создать до 1000 identity одной транзакцией.

        Вся пачка валидируется до транзакции, ошибки собираются в один `ValidationFailed`.
        `on_conflict="fail"` — при любой существующей identity `AlreadyExists` со списком ref,
        ничего не создаётся; `"skip"` — существующие попадают в `skipped`. Identity с тем же ref
        в другом пространстве — всегда `AlreadyExists`. Частичный успех и большие объёмы —
        `space.imports`.
        """
        mode = OnConflict(on_conflict)
        items = list(specs)
        if len(items) > CREATE_MANY_LIMIT:
            raise ValidationFailed(f"create_many: не больше {CREATE_MANY_LIMIT} записей; больше — imports")
        prepared = self._prepare_many(items)
        # единый порядок вставки: параллельные пересекающиеся пачки не дают deadlock
        ordered = sorted(prepared, key=lambda pair: (pair[1].row.service, pair[1].row.unique_ref))
        skipped: builtins.list[IdentityRef] = []
        async with self._space._tx() as tx:
            fresh: _Prepared = []
            for svc, item in ordered:
                existing = await self._space._existing(tx, item.row)
                if existing is None and await tx.insert_identity(item.row):
                    fresh.append((svc, item))
                    continue
                existing = existing or await self._space._existing(tx, item.row)
                found_id = existing.identity_id if existing else item.row.id
                skipped.append(IdentityRef(found_id, svc.name, item.row.external_ref))
            if skipped and mode is OnConflict.FAIL:
                refs = ", ".join(f"{ref.service}:{ref.external_ref}" for ref in skipped)
                raise AlreadyExists("identity", refs)
            # секреты и связи — после вставки всех строк: связь может вести на соседа по пачке
            for svc, item in fresh:
                await self._space._write_children_command(tx, item)
                await self._space._emit_registered(tx, svc, item, actor=actor)
            fresh_ids = {item.row.id for _svc, item in fresh}
            created = [
                await self._read(tx, item.row.id) for _svc, item in prepared if item.row.id in fresh_ids
            ]
        return CreateManyResult(created, skipped)

    # ------------------------------------------------------------------ изменение

    async def update(
        self,
        identity_id: UUID,
        *,
        profile: SchemaInput | None = None,
        labels: Mapping[str, Any] | None = None,
        note: str | _Unset | None = UNSET,
        actor: str,
        expected_revision: int | None = None,
    ) -> Identity:
        """Частичное изменение одной транзакцией и одним событием `identity.updated`.

        `profile` и `labels` сливаются с текущими; `None` в значении удаляет ключ. `note`
        заменяет заметку, `note=None` очищает её. Без реальных изменений событие не пишется.

        `expected_revision` — у всех команд изменения: ревизия, которую видел вызывающий.
        Расхождение — `RevisionConflict` без изменений; `None` — без проверки (D-206).
        """
        row = await self._at(identity_id, expected_revision)._update(
            identity_id, profile=profile, labels=labels, note=note, actor=actor
        )
        return Identity.from_row(row)

    async def replace_profile(
        self, identity_id: UUID, profile: SchemaInput, *, actor: str, expected_revision: int | None = None
    ) -> Identity:
        """Заменить профиль целиком (валидация схемой сервиса)."""
        space = self._at(identity_id, expected_revision)
        return Identity.from_row(await space._update_profile(identity_id, profile, replace=True, actor=actor))

    async def replace_attributes(
        self, identity_id: UUID, attributes: SchemaInput, *, actor: str, expected_revision: int | None = None
    ) -> Identity:
        """Заменить технические атрибуты целиком (валидация схемой сервиса)."""
        space = self._at(identity_id, expected_revision)
        return Identity.from_row(await space._set_attributes(identity_id, attributes, actor=actor))

    async def replace_labels(
        self,
        identity_id: UUID,
        labels: Mapping[str, Any],
        *,
        actor: str,
        expected_revision: int | None = None,
    ) -> Identity:
        """Заменить метки целиком; для слияния — `update(labels=...)`."""
        space = self._at(identity_id, expected_revision)
        return Identity.from_row(await space._set_labels(identity_id, labels, actor=actor))

    async def add_tags(
        self, identity_id: UUID, tags: Iterable[str], *, actor: str, expected_revision: int | None = None
    ) -> Identity:
        space = self._at(identity_id, expected_revision)
        return Identity.from_row(await space._add_tags(identity_id, tags, actor=actor))

    async def remove_tags(
        self, identity_id: UUID, tags: Iterable[str], *, actor: str, expected_revision: int | None = None
    ) -> Identity:
        space = self._at(identity_id, expected_revision)
        return Identity.from_row(await space._remove_tags(identity_id, tags, actor=actor))

    async def set_tags(
        self, identity_id: UUID, tags: Iterable[str], *, actor: str, expected_revision: int | None = None
    ) -> Identity:
        space = self._at(identity_id, expected_revision)
        return Identity.from_row(await space._set_tags(identity_id, tags, actor=actor))

    async def set_note(
        self, identity_id: UUID, note: str | None, *, actor: str, expected_revision: int | None = None
    ) -> Identity:
        space = self._at(identity_id, expected_revision)
        return Identity.from_row(await space._set_note(identity_id, note, actor=actor))

    async def update_many(
        self,
        target: IdentityFilter | Sequence[UUID],
        *,
        add_tags: Iterable[str] = (),
        remove_tags: Iterable[str] = (),
        set_labels: Mapping[str, Any] | None = None,
        lifecycle: Lifecycle | str | None = None,
        actor: str,
        limit: int = 1000,
    ) -> UpdateManyResult:
        """Атомарно изменить identity по фильтру или списку id.

        Охват больше ``limit`` отклоняется без изменений. Строки блокируются в
        порядке UUID; ошибка или запрещённый lifecycle-переход откатывает всю пачку.
        """
        if not 1 <= limit <= 1000:
            raise ValidationFailed("limit: от 1 до 1000")
        added = tuple(add_tags)
        removed = tuple(remove_tags)
        labels = None if set_labels is None else dict(set_labels)
        if not added and not removed and labels is None and lifecycle is None:
            raise ValidationFailed("update_many требует хотя бы одно изменение")
        lifecycle_value: Lifecycle | None = None
        if lifecycle is not None:
            try:
                lifecycle_value = Lifecycle(lifecycle)
            except ValueError as exc:
                raise ValidationFailed(f"неизвестный lifecycle: {lifecycle}") from exc
        if isinstance(target, IdentityFilter):
            search_filter = self._space._search_filter(self._query(target))
            ids = None
        else:
            search_filter = None
            ids = tuple(target)
        changed, skipped = await self._space._update_many(
            search_filter=search_filter,
            identity_ids=ids,
            add_tags=added,
            remove_tags=removed,
            set_labels=labels,
            lifecycle=lifecycle_value,
            actor=actor,
            limit=limit,
        )
        return UpdateManyResult(changed=changed, skipped=skipped)

    async def delete(
        self, identity_id: UUID, *, actor: str, revoke: bool = False, expected_revision: int | None = None
    ) -> None:
        """Физически удалить identity. Нельзя, пока на неё ссылаются связи; журнал сохраняется.

        При живой аренде — `ValidationFailed`; `revoke=True` отзывает её в той же транзакции.
        """
        await self._at(identity_id, expected_revision)._delete(identity_id, actor=actor, revoke=revoke)

    # ------------------------------------------------------------------ состояние

    async def activate(
        self,
        identity_id: UUID,
        *,
        actor: str,
        reason: str | None = None,
        expected_revision: int | None = None,
    ) -> RegionDecision:
        """Перевести в работу (`lifecycle → ready`); из `retired` нужна причина."""
        space = self._at(identity_id, expected_revision)
        return await space._set_lifecycle(identity_id, Lifecycle.READY, actor=actor, reason=reason)

    async def retire(
        self,
        identity_id: UUID,
        *,
        actor: str,
        reason: str | None = None,
        expected_revision: int | None = None,
    ) -> RegionDecision:
        """Обратимо вывести из работы (`lifecycle → retired`), данные сохраняются."""
        return await self._at(identity_id, expected_revision)._retire(identity_id, actor=actor, reason=reason)

    async def set_lifecycle(
        self,
        identity_id: UUID,
        state: Lifecycle | str,
        *,
        actor: str,
        reason: str | None = None,
        expected_revision: int | None = None,
    ) -> RegionDecision:
        space = self._at(identity_id, expected_revision)
        return await space._set_lifecycle(identity_id, state, actor=actor, reason=reason)

    async def restore_standing(
        self, identity_id: UUID, *, actor: str, reason: str, expected_revision: int | None = None
    ) -> RegionDecision:
        """Вернуть `banned → good` командой (sticky-бан наблюдения не снимают)."""
        space = self._at(identity_id, expected_revision)
        return await space._restore_standing(identity_id, actor=actor, reason=reason)

    async def ban(
        self,
        identity_id: UUID,
        *,
        reason: str,
        actor: str,
        expected_revision: int | None = None,
    ) -> RegionDecision:
        """Вручную установить `standing=banned` с теми же событиями, что у override."""
        return await self.override(
            identity_id,
            Override("standing", "", Standing.BANNED, reason=reason, actor=actor),
            expected_revision=expected_revision,
        )

    async def freeze(
        self,
        identity_id: UUID,
        *,
        reason: str,
        actor: str,
        expected_revision: int | None = None,
    ) -> RegionDecision:
        """Вручную установить `standing=frozen` с теми же событиями, что у override."""
        return await self.override(
            identity_id,
            Override("standing", "", Standing.FROZEN, reason=reason, actor=actor),
            expected_revision=expected_revision,
        )

    async def cool_down(
        self,
        identity_id: UUID,
        *,
        until: datetime | None = None,
        for_: timedelta | None = None,
        actor: str,
        reason: str | None = None,
        expected_revision: int | None = None,
    ) -> RegionDecision:
        """Временно исключить identity из выдачи; задайте ровно один срок `until` или `for_`."""
        space = self._at(identity_id, expected_revision)
        return await space._cool_down(identity_id, until=until, for_=for_, actor=actor, reason=reason)

    async def observe(
        self,
        identity_id: UUID,
        observation: Observation,
        *,
        actor: str,
        source: ChangeSource,
        expected_revision: int | None = None,
    ) -> RegionDecision:
        """Сообщить факт от платформы через ту же машину, что используют операции и пробы."""
        space = self._at(identity_id, expected_revision)
        return await space._public_observe(identity_id, observation, actor=actor, source=source)

    async def override(
        self, identity_id: UUID, override: Override, *, expected_revision: int | None = None
    ) -> RegionDecision:
        """Ручная правка региона админом; может закрепить регион (`pin_for`)."""
        return await self._at(identity_id, expected_revision)._override(identity_id, override)

    async def unpin(
        self,
        identity_id: UUID,
        region: str,
        key: str,
        *,
        actor: str,
        reason: str,
        expected_revision: int | None = None,
    ) -> RegionDecision:
        """Снять закрепление региона, не меняя состояния."""
        space = self._at(identity_id, expected_revision)
        return await space._unpin(identity_id, region, key, actor=actor, reason=reason)

    # ------------------------------------------------------------------ служебное

    def _prepare(self, spec: IdentityCreate) -> tuple[Service, PreparedIdentity]:
        svc = self._space.hub.service(spec.service)
        sessions = dict(spec.sessions)
        if spec.session is not None:
            if DEFAULT_SLOT in sessions:
                raise ValidationFailed("session и sessions['default'] заданы одновременно")
            sessions[DEFAULT_SLOT] = spec.session
        prepared = prepare_identity(
            self._space,
            svc,
            external_ref=spec.external_ref,
            identity_id=spec.id,
            credentials=spec.credentials,
            sessions=sessions,
            attributes=spec.attributes,
            profile=spec.profile,
            labels=spec.labels,
            tags=spec.tags,
            note=spec.note,
            links=spec.links,
            lifecycle=spec.lifecycle,
            auth_state=spec.auth,
            credential_records=spec.credential_records,
        )
        return svc, prepared

    def _prepare_many(self, items: builtins.list[IdentityCreate]) -> _Prepared:
        prepared: _Prepared = []
        errors: builtins.list[str] = []
        seen_refs: set[tuple[str, str]] = set()
        seen_ids: set[UUID] = set()
        for index, spec in enumerate(items):
            label = f"[{index}] {spec.service_name}:{spec.external_ref}"
            try:
                svc, item = self._prepare(spec)
            except (ValidationFailed, NotFound) as exc:
                errors.append(f"{label}: {exc}")
                continue
            key = (item.row.service, item.row.unique_ref)
            if key in seen_refs or item.row.id in seen_ids:
                errors.append(f"{label}: повтор в пачке")
                continue
            seen_refs.add(key)
            seen_ids.add(item.row.id)
            prepared.append((svc, item))
        if errors:
            raise ValidationFailed("; ".join(errors))
        return prepared

    @staticmethod
    async def _read(tx: StoreTx, identity_id: UUID) -> Identity:
        row = await tx.get_identity(identity_id)
        if row is None:  # pragma: no cover - вставлена в этой же транзакции
            raise NotFound("identity", identity_id)
        return Identity.from_row(row)

    def _query(self, where: IdentityFilter | None) -> SearchQuery:
        if where is None:
            return SearchQuery()
        names = where.services()
        services = [s for s in self._space.hub.services if not names or s.name in names]
        filterable = frozenset().union(*(s.filterable_profile for s in services)) if services else frozenset()
        return where.to_query(frozenset(filterable))

    async def _page(self, query: SearchQuery, limit: int, after: str | None, count: bool) -> Page[Identity]:
        found = await self._space._search(query, limit=limit, after=after, count=count)
        return Page([Identity.from_row(row) for row in found.items], found.next_cursor, found.total)
