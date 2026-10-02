"""Identity Hub — встроенные таблицы библиотеки (SQLAlchemy 2.0, PostgreSQL).

Все таблицы с префиксом ih_ и собственным MetaData, чтобы не смешиваться
с таблицами приложения и мигрироваться отдельной веткой Alembic.

space_id денормализован во все дочерние таблицы: это нужно, чтобы
Row-Level Security фильтровал по одной колонке без join'ов.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Interval,
    LargeBinary,
    MetaData,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.dialects.postgresql.base import ischema_names
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import UserDefinedType

from identity_hub._ids import new_id
from identity_hub.enums import (
    AuthState,
    Availability,
    ChangeSource,
    DeliveryStatus,
    LeaseEndReason,
    Lifecycle,
    ProxyStatus,
    SessionStatus,
    Severity,
    Standing,
)

__all__ = [
    "BindingRecord",
    "ChallengeRecord",
    "CredentialsRecord",
    "EncryptedPayloadMixin",
    "EventCursorRecord",
    "EventRecord",
    "IdentityDataRecord",
    "IdentityHubBase",
    "IdentityLinkRecord",
    "IdentityRecord",
    "ImportItemRecord",
    "ImportRunRecord",
    "LeaseHistoryRecord",
    "LeaseLogRecord",
    "LeaseRecord",
    "NotificationChannelRecord",
    "NotificationRecord",
    "NotificationRuleRecord",
    "OperationKeyRecord",
    "ProviderOperationRecord",
    "ProxyRecord",
    "QuotaUsageRecord",
    "RegionStateRecord",
    "ServiceRecord",
    "SessionRecord",
    "SpaceRecord",
    "StateSnapshotCoverageRecord",
    "StateSnapshotRecord",
    "StatsMetricBatchRecord",
    "StatsRollupRecord",
    "VerificationRecord",
    "WebhookDeliveryRecord",
    "WebhookEndpointRecord",
]


class XID8(UserDefinedType[str]):
    """Тип Postgres xid8 (PG13+): 64-битный идентификатор транзакции."""

    cache_ok = True

    def get_col_spec(self, **kw: Any) -> str:
        return "XID8"


# рефлексия (Alembic autogenerate, doctor) распознаёт xid8
ischema_names["xid8"] = XID8


class IdentityHubBase(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_name)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


# Состояние аккаунта — пять ортогональных регионов, а не один статус (enums.py).
# Синхронизируемые с внешним миром (standing, auth, verification, proxy) имеют
# служебную запись в ih_region_states; на ih_identities — денормализованные
# значения для быстрой выборки аренды.


def _enum_check(column: str, enum: type[StrEnum]) -> CheckConstraint:
    values = ", ".join(f"'{member.value}'" for member in enum)
    return CheckConstraint(f"{column} IN ({values})", name=f"{column}_valid")


# ---------------------------------------------------------------- шифрование


class EncryptedPayloadMixin:
    """Envelope encryption: DEK на запись, зашифрованный мастер-ключом key_id.

    AES-GCM с associated data = (таблица, identity_id, slot), чтобы blob
    нельзя было перенести в чужую строку.
    """

    key_id: Mapped[str] = mapped_column(String(64))
    wrapped_dek: Mapped[bytes] = mapped_column(LargeBinary)
    nonce: Mapped[bytes] = mapped_column(LargeBinary)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)


# ---------------------------------------------------------------- пространства и сервисы


class SpaceRecord(IdentityHubBase):
    """Граница изоляции: проект или человек. Всё остальное принадлежит пространству."""

    __tablename__ = "ih_spaces"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=new_id)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    title: Mapped[str] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(64), server_default=text("'UTC'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ServiceRecord(IdentityHubBase):
    """Реестр сервисов для админки. Источник правды — объекты Service в коде,
    строка синхронизируется при старте приложения."""

    __tablename__ = "ih_services"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ImportRunRecord(IdentityHubBase):
    """Durable, secret-free progress and report for one admin import."""

    __tablename__ = "ih_import_runs"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=new_id)
    space_id: Mapped[UUID] = mapped_column(ForeignKey("ih_spaces.id", ondelete="CASCADE"))
    service: Mapped[str] = mapped_column(ForeignKey("ih_services.name", ondelete="RESTRICT"))
    actor: Mapped[str] = mapped_column(String(256))
    source: Mapped[str] = mapped_column(String(256))
    state: Mapped[str] = mapped_column(String(16), default="pending", server_default=text("'pending'"))
    on_existing: Mapped[str] = mapped_column(String(24), default="skip", server_default=text("'skip'"))
    verify_sessions: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    labels: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    total: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    processed: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    created: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    updated: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    skipped: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    failed: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    report: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))

    __table_args__ = (
        UniqueConstraint("id", "space_id", name="uq_ih_import_runs_id_space"),
        CheckConstraint(
            "state IN ('pending', 'running', 'succeeded', 'failed', 'cancelling', 'cancelled')",
            name="state_valid",
        ),
        CheckConstraint("on_existing IN ('skip', 'update_session', 'fail')", name="on_existing_valid"),
        CheckConstraint(
            "total >= 0 AND processed >= 0 AND created >= 0 AND updated >= 0 "
            "AND skipped >= 0 AND failed >= 0",
            name="counts_nonnegative",
        ),
        Index("ix_ih_import_runs_space_started", "space_id", "started_at"),
        Index(
            "ix_ih_import_runs_active",
            "updated_at",
            postgresql_where=text("state IN ('pending', 'running', 'cancelling')"),
        ),
    )


class ImportItemRecord(EncryptedPayloadMixin, IdentityHubBase):
    """Encrypted normalized input retained only while its import can resume."""

    __tablename__ = "ih_import_items"

    run_id: Mapped[UUID] = mapped_column(primary_key=True)
    position: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    space_id: Mapped[UUID]
    state: Mapped[str] = mapped_column(String(16), default="pending", server_default=text("'pending'"))
    claim_token: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    claimed_by: Mapped[str | None] = mapped_column(String(256))
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "space_id"],
            ["ih_import_runs.id", "ih_import_runs.space_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("position >= 0", name="position_nonnegative"),
        CheckConstraint(
            "state IN ('pending', 'running', 'succeeded', 'skipped', 'failed')",
            name="state_valid",
        ),
        Index(
            "ix_ih_import_items_claim",
            "run_id",
            "position",
            postgresql_where=text("state IN ('pending', 'running')"),
        ),
    )


# ---------------------------------------------------------------- идентичности


def _default_normalized_ref(context: Any) -> str:
    """Вставка без normalized_ref (ORM, фикстуры) — нормализация по умолчанию `str.strip` (D-149)."""
    return str(context.get_current_parameters()["external_ref"]).strip()


class IdentityRecord(IdentityHubBase):
    __tablename__ = "ih_identities"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=new_id)
    space_id: Mapped[UUID] = mapped_column(ForeignKey("ih_spaces.id", ondelete="RESTRICT"))
    service: Mapped[str] = mapped_column(ForeignKey("ih_services.name", ondelete="RESTRICT"))
    external_ref: Mapped[str] = mapped_column(String(256))  # телефон, логин, email — как ввели
    # результат Service.normalize_ref: уникальность и поиск (IMP-08, миграция 0008)
    normalized_ref: Mapped[str] = mapped_column(String(256), default=_default_normalized_ref)
    normalize_version: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))

    # --- регионы состояния (денормализовано для выборки; источник правды для
    #     синхронизируемых регионов — ih_region_states)
    lifecycle: Mapped[str] = mapped_column(String(16), default=Lifecycle.PROVISIONING)
    standing: Mapped[str] = mapped_column(String(16), default=Standing.GOOD)
    standing_code: Mapped[str | None] = mapped_column(
        String(64)
    )  # код платформы: spam_restricted, checkpoint
    standing_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    auth_state: Mapped[str] = mapped_column(String(24), default=AuthState.NO_SESSION)
    availability: Mapped[str] = mapped_column(String(16), default=Availability.AVAILABLE)
    availability_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Что платформа требует сейчас (по образцу Stripe requirements):
    # [{"code": "phone_verification", "deadline": "...", "since": "..."}]
    requirements: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))

    tags: Mapped[list[str]] = mapped_column(ARRAY(String(64)), server_default=text("'{}'"))
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )  # техника: device, UA
    profile: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )  # персона: имя, ДР, страна
    labels: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )  # плоские key→scalar для фильтров
    note: Mapped[str | None] = mapped_column(Text)  # заметка из админки

    # Денормализованная строка для текстового поиска: external_ref, короткий id,
    # поля profile, объявленные сервисом как searchable, значения labels, note.
    # Поддерживается библиотекой при каждой записи; нужен pg_trgm.
    search_text: Mapped[str] = mapped_column(Text, server_default=text("''"))

    # Кэш подтверждённых факторов для быстрой выборки аренды (verified_factors @> '{kyc_id}').
    # Поддерживается библиотекой при каждом переходе в ih_verifications.
    verified_factors: Mapped[list[str]] = mapped_column(ARRAY(String(64)), server_default=text("'{}'"))

    # нефильтруемая часть профиля при Service.profile_encrypted (D-109, миграция 0002)
    profile_key_id: Mapped[str | None] = mapped_column(String(64))
    profile_wrapped_dek: Mapped[bytes | None] = mapped_column(LargeBinary)
    profile_nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    profile_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)

    error_score: Mapped[float] = mapped_column(Float, default=0.0)
    fencing_seq: Mapped[int] = mapped_column(BigInteger, default=0)  # растёт с каждой арендой
    event_seq: Mapped[int] = mapped_column(BigInteger, default=0)  # номер последнего события аккаунта

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        # глобально, а не в пространстве: один аккаунт не должен жить в двух пространствах
        UniqueConstraint("service", "normalized_ref", name="uq_ih_identities_service_normalized_ref"),
        # составной ключ для FK из дочерних таблиц — гарантирует совпадение space_id
        UniqueConstraint("id", "space_id", name="uq_ih_identities_id_space"),
        _enum_check("lifecycle", Lifecycle),
        _enum_check("standing", Standing),
        _enum_check("auth_state", AuthState),
        _enum_check("availability", Availability),
        # выборка аренды: только готовые, авторизованные, не забаненные
        Index(
            "ix_ih_identities_pick",
            "space_id",
            "service",
            "availability",
            postgresql_where=text(
                "lifecycle = 'ready' AND auth_state = 'authenticated' AND standing IN ('good', 'limited')"
            ),
        ),
        Index("ix_ih_identities_regions", "space_id", "service", "lifecycle", "standing", "auth_state"),
        Index("ix_ih_identities_tags", "tags", postgresql_using="gin"),
        Index("ix_ih_identities_verified", "verified_factors", postgresql_using="gin"),
        # точные фильтры: labels @> '{"farm": "3"}', profile @> '{"country": "UA"}'
        Index(
            "ix_ih_identities_labels",
            "labels",
            postgresql_using="gin",
            postgresql_ops={"labels": "jsonb_path_ops"},
        ),
        Index(
            "ix_ih_identities_profile",
            "profile",
            postgresql_using="gin",
            postgresql_ops={"profile": "jsonb_path_ops"},
        ),
        # подстрочный поиск: search_text ILIKE '%olena%'
        Index(
            "ix_ih_identities_search",
            "search_text",
            postgresql_using="gin",
            postgresql_ops={"search_text": "gin_trgm_ops"},
        ),
        # keyset-пагинация списка в админке
        Index("ix_ih_identities_list", "space_id", "created_at", "id"),
        # ленивое снятие временных состояний
        Index(
            "ix_ih_identities_availability_until",
            "availability_until",
            postgresql_where=text("availability_until IS NOT NULL"),
        ),
        Index(
            "ix_ih_identities_standing_until",
            "standing_until",
            postgresql_where=text("standing_until IS NOT NULL"),
        ),
    )


def _identity_fk() -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["identity_id", "space_id"],
        ["ih_identities.id", "ih_identities.space_id"],
        ondelete="CASCADE",
    )


# ---------------------------------------------------------------- состояния регионов


class RegionStateRecord(IdentityHubBase):
    """Служебная запись синхронизируемого региона: источник правды и метаданные сверки.

    region: standing | auth | verification | requirement
    key:    '' для standing, slot сессии для auth, factor для verification,
            code требования для requirement.

    Денормализованные значения на ih_identities обновляются в той же транзакции.
    """

    __tablename__ = "ih_region_states"

    identity_id: Mapped[UUID] = mapped_column(primary_key=True)
    region: Mapped[str] = mapped_column(String(32), primary_key=True)
    key: Mapped[str] = mapped_column(String(64), primary_key=True, default="")
    space_id: Mapped[UUID]

    state: Mapped[str] = mapped_column(String(32))
    code: Mapped[str | None] = mapped_column(String(64))
    until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    change_source: Mapped[str] = mapped_column(String(16), default=ChangeSource.COMMAND)
    changed_by: Mapped[str | None] = mapped_column(String(256))  # воркер или пользователь админки
    change_reason: Mapped[str | None] = mapped_column(Text)  # обязательно для override

    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_status: Mapped[str | None] = mapped_column(String(128))  # как сказала платформа
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pending_observation: Mapped[str | None] = mapped_column(String(32))  # для confirm_leaving
    pinned_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # после override

    version: Mapped[int] = mapped_column(BigInteger, default=1)

    __table_args__ = (
        _identity_fk(),
        CheckConstraint(
            "change_source IN (" + ", ".join(f"'{c.value}'" for c in ChangeSource) + ")",
            name="change_source_valid",
        ),
        CheckConstraint(
            "change_source <> 'override' OR change_reason IS NOT NULL",
            name="override_has_reason",
        ),
        Index("ix_ih_region_states_state", "space_id", "region", "key", "state"),
        # кандидаты на сверку: давно не сверялись
        Index("ix_ih_region_states_synced", "region", "synced_at"),
        Index(
            "ix_ih_region_states_pinned", "pinned_until", postgresql_where=text("pinned_until IS NOT NULL")
        ),
    )


# ---------------------------------------------------------------- связи между аккаунтами


class IdentityLinkRecord(IdentityHubBase):
    """Направленная связь аккаунт → аккаунт с ролью.

    Пример: instagram-аккаунт --recovery_email--> gmail-аккаунт.
    Роль — строка, её смысл задаёт сервис (какие роли он понимает при входе/рекавери).
    """

    __tablename__ = "ih_identity_links"

    identity_id: Mapped[UUID] = mapped_column(primary_key=True)  # зависимый
    role: Mapped[str] = mapped_column(String(64), primary_key=True)  # recovery_email, registered_phone
    space_id: Mapped[UUID]
    linked_identity_id: Mapped[UUID]  # тот, от кого зависят
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        _identity_fk(),
        # связанный аккаунт из того же пространства; удалить его, пока на него ссылаются, нельзя
        ForeignKeyConstraint(
            ["linked_identity_id", "space_id"],
            ["ih_identities.id", "ih_identities.space_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("identity_id <> linked_identity_id", name="not_self"),
        Index("ix_ih_identity_links_linked", "linked_identity_id"),  # «кто зависит от этой почты»
    )


# ---------------------------------------------------------------- пользовательские данные


class IdentityDataRecord(IdentityHubBase):
    """Произвольные данные пользователя библиотеки, по неймспейсам.

    Escape hatch для небольшого, редко агрегируемого состояния
    (например, статистика одной игры). Схему задаёт Pydantic-модель
    на стороне пользователя, библиотека хранит JSON и версию.
    """

    __tablename__ = "ih_identity_data"

    identity_id: Mapped[UUID] = mapped_column(primary_key=True)
    namespace: Mapped[str] = mapped_column(String(128), primary_key=True)  # "farm.hamster", "farm.notcoin"
    space_id: Mapped[UUID]
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    version: Mapped[int] = mapped_column(BigInteger, default=1)  # оптимистическая блокировка
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        _identity_fk(),
        Index("ix_ih_identity_data_namespace", "space_id", "namespace"),
    )


class VerificationRecord(IdentityHubBase):
    """Специфичные данные одной верификации аккаунта.

    factor — имя из VerificationSpec сервиса (email, phone, kyc_id, liveness...).
    Само состояние, код и метаданные сверки — в ih_region_states
    (region='verification', key=factor); здесь то, чего нет у других регионов.
    """

    __tablename__ = "ih_verifications"

    identity_id: Mapped[UUID] = mapped_column(primary_key=True)
    factor: Mapped[str] = mapped_column(String(64), primary_key=True)
    space_id: Mapped[UUID]

    attempts: Mapped[int] = mapped_column(Integer, default=0)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    via_identity_id: Mapped[UUID | None]  # чем подтверждено: связанная почта или номер
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))  # не секретное

    # секретные доказательства (номер документа и т.п.), если сервис их объявил
    evidence_key_id: Mapped[str | None] = mapped_column(String(64))
    evidence_wrapped_dek: Mapped[bytes | None] = mapped_column(LargeBinary)
    evidence_nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    evidence_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)

    __table_args__ = (
        _identity_fk(),
        Index(
            "ix_ih_verifications_expires_at",
            "expires_at",
            postgresql_where=text("expires_at IS NOT NULL"),
        ),
    )


# ---------------------------------------------------------------- авторизация


class CredentialsRecord(EncryptedPayloadMixin, IdentityHubBase):
    """Как войти: пароль, телефон, TOTP-секрет. Одна запись на идентичность."""

    __tablename__ = "ih_credentials"

    identity_id: Mapped[UUID] = mapped_column(primary_key=True)
    space_id: Mapped[UUID]
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (_identity_fk(),)


class SessionRecord(EncryptedPayloadMixin, IdentityHubBase):
    """Результат входа: StringSession, cookies, OAuth-токены.

    slot позволяет держать несколько авторизаций одного аккаунта.
    """

    __tablename__ = "ih_sessions"

    identity_id: Mapped[UUID] = mapped_column(primary_key=True)
    slot: Mapped[str] = mapped_column(String(64), primary_key=True, default="default")
    space_id: Mapped[UUID]

    status: Mapped[str] = mapped_column(String(16), default=SessionStatus.VALID)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1)
    written_by_fencing: Mapped[int] = mapped_column(BigInteger)  # запись отклоняется, если меньше текущего
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        _identity_fk(),
        _enum_check("status", SessionStatus),
        Index(
            "ix_ih_sessions_expires_at",
            "expires_at",
            postgresql_where=text("expires_at IS NOT NULL"),
        ),
    )


class ChallengeRecord(IdentityHubBase):
    """Вопрос платформы при входе, на который должен ответить человек."""

    __tablename__ = "ih_challenges"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=new_id)
    identity_id: Mapped[UUID]
    space_id: Mapped[UUID]

    kind: Mapped[str] = mapped_column(String(32))  # sms_code, captcha, email_confirm
    hint: Mapped[str] = mapped_column(Text)

    # context (phone_code_hash и т.п.) и answer — секреты, храним зашифрованными
    context_key_id: Mapped[str] = mapped_column(String(64))
    context_wrapped_dek: Mapped[bytes] = mapped_column(LargeBinary)
    context_nonce: Mapped[bytes] = mapped_column(LargeBinary)
    context_ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    answer_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    answer_nonce: Mapped[bytes | None] = mapped_column(LargeBinary)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        _identity_fk(),
        # не больше одного открытого вопроса на аккаунт
        Index(
            "uq_ih_challenges_open",
            "identity_id",
            unique=True,
            postgresql_where=text("resolved_at IS NULL"),
        ),
    )


# ---------------------------------------------------------------- аренда


class LeaseRecord(IdentityHubBase):
    """Эксклюзивная аренда. PK = identity_id: у аккаунта не больше одной строки.

    Строка с истёкшим expires_at считается свободной и перезаписывается
    следующим арендатором с новым fencing.
    """

    __tablename__ = "ih_leases"

    identity_id: Mapped[UUID] = mapped_column(primary_key=True)
    space_id: Mapped[UUID]

    lease_id: Mapped[UUID] = mapped_column(unique=True)  # публичный id = ih_lease_history.lease_id
    token: Mapped[UUID] = mapped_column(unique=True, default=uuid4)  # приватный: только для extend/release
    fencing: Mapped[int] = mapped_column(BigInteger)
    pool: Mapped[str] = mapped_column(String(128))  # для админки: кто и зачем держит
    holder: Mapped[str] = mapped_column(String(256))  # hostname:pid:worker
    idempotency_key: Mapped[str | None] = mapped_column(String(256))

    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        _identity_fk(),
        Index("ix_ih_leases_expires_at", "expires_at"),
        # поиск живой аренды по ключу в пределах пространства; единственность даёт ih_operation_keys
        Index(
            "ix_ih_leases_idempotency",
            "space_id",
            "idempotency_key",
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )


class LeaseHistoryRecord(IdentityHubBase):
    """Одна строка на одну аренду: когда взяли, кто, сколько держал, чем закончилось.

    Строка создаётся в той же транзакции, что и запись в ih_leases,
    и закрывается при освобождении или при обнаружении истечения.
    Без FK на ih_identities: история переживает удаление аккаунта.
    """

    __tablename__ = "ih_lease_history"

    lease_id: Mapped[UUID] = mapped_column(primary_key=True, default=new_id)  # публичный id аренды
    space_id: Mapped[UUID]
    identity_id: Mapped[UUID]
    token: Mapped[UUID] = mapped_column(unique=True)  # связь с ih_leases.token
    fencing: Mapped[int] = mapped_column(BigInteger)

    pool: Mapped[str] = mapped_column(String(128))
    holder: Mapped[str] = mapped_column(String(256))
    proxy_id: Mapped[UUID | None]  # через какой прокси работали

    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration: Mapped[timedelta | None] = mapped_column(
        Interval, Computed("released_at - acquired_at", persisted=True)
    )
    end_reason: Mapped[str | None] = mapped_column(String(16))

    # счётчики копятся в памяти воркера и сбрасываются вместе с heartbeat
    operations: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )  # {"resolve": 12}
    succeeded: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "end_reason IS NULL OR end_reason IN (" + ", ".join(f"'{r.value}'" for r in LeaseEndReason) + ")",
            name="end_reason_valid",
        ),
        Index("ix_ih_lease_history_identity", "identity_id", "acquired_at"),
        Index("ix_ih_lease_history_space_acquired", "space_id", "acquired_at"),
        Index("ix_ih_lease_history_space_released", "space_id", "released_at"),
        Index("ix_ih_lease_history_space_holder_acquired", "space_id", "holder", "acquired_at"),
        Index(
            "ix_ih_lease_history_open",
            "last_heartbeat_at",
            postgresql_where=text("released_at IS NULL"),
        ),
        # чистильщик истёкших аренд идёт от открытой истории своего пространства (миграция 0011)
        Index(
            "ix_ih_lease_history_open_space",
            "space_id",
            "lease_id",
            postgresql_where=text("released_at IS NULL"),
        ),
    )


class LeaseLogRecord(IdentityHubBase):
    """Журнал аренды в Postgres — опциональный sink для важных, негромких записей.

    Основной поток логов идёт в обычный logging/structlog с lease_id в контексте.
    Сюда попадает только то, что прошло фильтр sink'а (уровень, лимит строк на аренду).
    Пишется пачками: буфер в памяти сбрасывается на heartbeat и release.
    """

    __tablename__ = "ih_lease_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    lease_id: Mapped[UUID]
    identity_id: Mapped[UUID]
    space_id: Mapped[UUID]
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # время на воркере
    level: Mapped[int] = mapped_column(Integer)  # logging.INFO = 20
    message: Mapped[str] = mapped_column(Text)
    fields: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))

    __table_args__ = (
        Index("ix_ih_lease_logs_lease", "lease_id", "id"),
        Index("ix_ih_lease_logs_identity_ts", "identity_id", "ts"),
        Index("ix_ih_lease_logs_ts", "ts"),  # для чистки по сроку хранения
    )


# ---------------------------------------------------------------- квоты и операции


class QuotaUsageRecord(IdentityHubBase):
    """Счётчик фиксированного окна. window_seconds в ключе, потому что у одного
    вида операции может быть несколько правил (в час и в сутки).

    Pacing использует ту же таблицу с kind = '__pacing__'.
    """

    __tablename__ = "ih_quota_usage"

    identity_id: Mapped[UUID] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_seconds: Mapped[int] = mapped_column(Integer, primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    space_id: Mapped[UUID]
    used: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        _identity_fk(),
        Index("ix_ih_quota_usage_window_start", "window_start"),  # для чистки старых окон
    )


class OperationKeyRecord(IdentityHubBase):
    """Ключи идемпотентности (IMP-03): повтор команды с тем же ключом не выполняет её дважды.

    `namespace` — пространство и семейство команд (`<space_id>:operation`); ключ уникален в нём.
    """

    __tablename__ = "ih_operation_keys"

    namespace: Mapped[str] = mapped_column(String(128), primary_key=True)
    key: Mapped[str] = mapped_column(String(256), primary_key=True)
    # аккаунт последней попытки; без FK: ключ переживает удаление аккаунта до expires_at (D-153)
    identity_id: Mapped[UUID | None]
    space_id: Mapped[UUID]
    operation: Mapped[str] = mapped_column(String(64))
    fingerprint_version: Mapped[int] = mapped_column(SmallInteger)  # 0 — запись до протокола
    fingerprint: Mapped[str | None] = mapped_column(String(64))
    attempt: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str | None] = mapped_column(String(32))  # None — выполняется или исход неизвестен
    result_type: Mapped[str | None] = mapped_column(String(64))
    result_version: Mapped[int | None] = mapped_column(SmallInteger)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_ih_operation_keys_expires_at", "expires_at"),)  # чистка истёкших ключей


class ProviderOperationRecord(IdentityHubBase):
    """Durable claim/call/apply state for login, refresh and probe adapters."""

    __tablename__ = "ih_provider_operations"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=new_id)
    space_id: Mapped[UUID]
    identity_id: Mapped[UUID]
    kind: Mapped[str] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(16), default="pending", server_default=text("'pending'"))
    attempt: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    claim_id: Mapped[UUID | None]
    claim_token: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    claimed_by: Mapped[str | None] = mapped_column(String(256))
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        _identity_fk(),
        CheckConstraint("attempt >= 0 AND claim_token >= 0", name="counters_nonnegative"),
        CheckConstraint(
            "state IN ('pending', 'running', 'retryable', 'succeeded', 'failed', 'cancelled')",
            name="state_valid",
        ),
        Index(
            "ix_ih_provider_operations_claim",
            "available_at",
            postgresql_where=text("state IN ('pending', 'retryable')"),
        ),
        Index("ix_ih_provider_operations_identity", "identity_id", "created_at"),
        Index(
            "ix_ih_provider_operations_stale",
            "claim_expires_at",
            postgresql_where=text("state = 'running'"),
        ),
    )


# ---------------------------------------------------------------- прокси


class ProxyRecord(EncryptedPayloadMixin, IdentityHubBase):
    """Прокси. host/port открыто (для админки и дедупликации),
    логин и пароль — в зашифрованном payload."""

    __tablename__ = "ih_proxies"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=new_id)
    space_id: Mapped[UUID] = mapped_column(ForeignKey("ih_spaces.id", ondelete="RESTRICT"))
    pool: Mapped[str] = mapped_column(String(128))  # residential-ua, datacenter-de
    scheme: Mapped[str] = mapped_column(String(16))  # socks5, http, mtproxy
    host: Mapped[str] = mapped_column(String(256))
    port: Mapped[int] = mapped_column(Integer)
    fingerprint: Mapped[str] = mapped_column(String(64))  # hash(scheme, host, port, username)

    status: Mapped[str] = mapped_column(String(16), default=ProxyStatus.ALIVE)
    error_score: Mapped[float] = mapped_column(Float, default=0.0)
    fail_streak: Mapped[int] = mapped_column(Integer, default=0)  # для экспоненциального бэкоффа
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # синхронизация здоровья прокси (тот же принцип, что у регионов аккаунта)
    status_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    status_source: Mapped[str] = mapped_column(String(16), default=ChangeSource.COMMAND)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pinned_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pending_observation: Mapped[str | None] = mapped_column(
        String(16)
    )  # confirm_leaving (D-116, миграция 0003)
    max_identities: Mapped[int] = mapped_column(Integer, default=3)
    bound_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )  # D-117, миграция 0004

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("space_id", "fingerprint", name="uq_ih_proxies_space_fingerprint"),
        UniqueConstraint("id", "space_id", name="uq_ih_proxies_id_space"),
        _enum_check("status", ProxyStatus),
        Index("ix_ih_proxies_pick", "space_id", "pool", "status"),
        Index("ix_ih_proxies_host", "host", postgresql_using="gin", postgresql_ops={"host": "gin_trgm_ops"}),
    )


class BindingRecord(IdentityHubBase):
    """Постоянная пара аккаунт ↔ прокси (sticky)."""

    __tablename__ = "ih_bindings"

    identity_id: Mapped[UUID] = mapped_column(primary_key=True)
    space_id: Mapped[UUID]
    proxy_id: Mapped[UUID]
    bound_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        _identity_fk(),
        # прокси из того же пространства, что и аккаунт
        ForeignKeyConstraint(
            ["proxy_id", "space_id"],
            ["ih_proxies.id", "ih_proxies.space_id"],
            ondelete="RESTRICT",
        ),
        Index("ix_ih_bindings_proxy_id", "proxy_id"),  # загрузка прокси
    )


# ---------------------------------------------------------------- события


class EventRecord(IdentityHubBase):
    """Журнал событий = transactional outbox.

    Пишется в той же транзакции, что и изменение. Потребители читают по курсору
    (tx_id, id) и только ниже горизонта видимости pg_snapshot_xmin, чтобы не
    пропустить события транзакций, закоммиченных позже с меньшим id.
    """

    __tablename__ = "ih_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[UUID] = mapped_column(unique=True, default=new_id)  # ключ идемпотентности получателей
    tx_id: Mapped[str] = mapped_column(XID8, server_default=text("pg_current_xact_id()"))

    type: Mapped[str] = mapped_column(String(64))  # identity.standing.changed
    severity: Mapped[str] = mapped_column(String(16), default=Severity.INFO)
    space_id: Mapped[UUID] = mapped_column(ForeignKey("ih_spaces.id", ondelete="CASCADE"))
    service: Mapped[str | None] = mapped_column(String(64))
    identity_id: Mapped[UUID | None]  # без FK: журнал переживает удаление
    proxy_id: Mapped[UUID | None]
    lease_id: Mapped[UUID | None]
    sequence: Mapped[int | None] = mapped_column(BigInteger)  # event_seq аккаунта на момент события
    actor: Mapped[str] = mapped_column(String(256))  # воркер, пользователь админки, platform
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))  # без секретов
    external: Mapped[bool] = mapped_column(default=True)  # False — только журнал (lease.*, operation.*)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        _enum_check("severity", Severity),
        Index("ix_ih_events_cursor", "tx_id", "id", postgresql_where=text("external")),
        Index("ix_ih_events_identity", "identity_id", "occurred_at"),
        Index("ix_ih_events_identity_type_occurred", "identity_id", "type", "occurred_at"),
        Index("ix_ih_events_space_occurred", "space_id", "occurred_at"),
        Index("ix_ih_events_type", "space_id", "type", "occurred_at"),
    )


class EventCursorRecord(IdentityHubBase):
    """Позиция потребителя в журнале. Один активный экземпляр на потребителя
    обеспечивается блокировкой строки (FOR UPDATE SKIP LOCKED) на время пачки."""

    __tablename__ = "ih_event_cursors"

    consumer: Mapped[str] = mapped_column(
        String(128), primary_key=True
    )  # webhooks, notifications, callbacks:<name>
    last_tx_id: Mapped[str] = mapped_column(XID8, server_default=text("'0'::xid8"))
    last_id: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StatsRollupRecord(IdentityHubBase):
    """Absolute event aggregates; unique keys make dispatcher retries idempotent."""

    __tablename__ = "ih_stats_rollup"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    space_id: Mapped[UUID] = mapped_column(ForeignKey("ih_spaces.id", ondelete="CASCADE"))
    service: Mapped[str] = mapped_column(String(64), default="")
    metric: Mapped[str] = mapped_column(String(128))
    dims: Mapped[dict[str, str]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    bucket_size: Mapped[str] = mapped_column(String(2))
    value: Mapped[float] = mapped_column(Float, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint("bucket_size IN ('1m', '1h', '1d')", name="bucket_size_valid"),
        UniqueConstraint(
            "space_id",
            "service",
            "metric",
            "dims",
            "bucket_start",
            "bucket_size",
            name="uq_ih_stats_rollup_key",
        ),
        Index(
            "ix_ih_stats_rollup_series",
            "space_id",
            "metric",
            "bucket_size",
            "bucket_start",
        ),
        Index(
            "ix_ih_stats_rollup_minute_retention",
            "bucket_start",
            postgresql_where=text("bucket_size = '1m'"),
        ),
    )


class StatsMetricBatchRecord(IdentityHubBase):
    """Exactly-once receipt for one process-local buffered MetricsHook flush."""

    __tablename__ = "ih_stats_metric_batches"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StateSnapshotCoverageRecord(IdentityHubBase):
    """One explicit coverage marker per space/hour, including empty spaces."""

    __tablename__ = "ih_state_snapshot_coverage"

    space_id: Mapped[UUID] = mapped_column(ForeignKey("ih_spaces.id", ondelete="CASCADE"), primary_key=True)
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    source: Mapped[str] = mapped_column(String(16))
    complete: Mapped[bool] = mapped_column(default=True)


class StateSnapshotRecord(IdentityHubBase):
    """Count for one service/region/state in an explicitly covered hour."""

    __tablename__ = "ih_state_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    space_id: Mapped[UUID]
    service: Mapped[str] = mapped_column(String(64))
    region: Mapped[str] = mapped_column(String(192))
    state: Mapped[str] = mapped_column(String(64))
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    count: Mapped[int] = mapped_column(Integer)

    __table_args__ = (
        CheckConstraint("count >= 0", name="count_nonnegative"),
        ForeignKeyConstraint(
            ["space_id", "taken_at"],
            ["ih_state_snapshot_coverage.space_id", "ih_state_snapshot_coverage.taken_at"],
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "space_id",
            "service",
            "region",
            "state",
            "taken_at",
            name="uq_ih_state_snapshots_key",
        ),
        Index(
            "ix_ih_state_snapshots_series",
            "space_id",
            "region",
            "taken_at",
        ),
    )


# ---------------------------------------------------------------- исходящие вебхуки


class WebhookEndpointRecord(EncryptedPayloadMixin, IdentityHubBase):
    """Получатель событий. Секрет подписи HMAC — в зашифрованном payload."""

    __tablename__ = "ih_webhook_endpoints"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=new_id)
    space_id: Mapped[UUID] = mapped_column(ForeignKey("ih_spaces.id", ondelete="CASCADE"))
    url: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    event_types: Mapped[list[str]] = mapped_column(ARRAY(String(64)))  # поддерживает маски: pool.capacity.*
    filter: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )  # service, labels
    min_severity: Mapped[str] = mapped_column(String(16), default=Severity.INFO)

    enabled: Mapped[bool] = mapped_column(default=True)
    disabled_reason: Mapped[str | None] = mapped_column(Text)
    failure_streak: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (_enum_check("min_severity", Severity),)


class WebhookDeliveryRecord(IdentityHubBase):
    """Одна доставка одного события одному endpoint'у, с ретраями."""

    __tablename__ = "ih_webhook_deliveries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    endpoint_id: Mapped[UUID] = mapped_column(ForeignKey("ih_webhook_endpoints.id", ondelete="CASCADE"))
    event_id: Mapped[UUID]
    status: Mapped[str] = mapped_column(String(16), default=DeliveryStatus.PENDING)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    response_code: Mapped[int | None] = mapped_column(Integer)
    response_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("endpoint_id", "event_id", name="uq_ih_webhook_deliveries_endpoint_event"),
        _enum_check("status", DeliveryStatus),
        Index(
            "ix_ih_webhook_deliveries_due",
            "next_attempt_at",
            postgresql_where=text("status IN ('pending', 'failed')"),
        ),
    )


# ---------------------------------------------------------------- уведомления (Telegram)


class NotificationChannelRecord(EncryptedPayloadMixin, IdentityHubBase):
    """Канал доставки уведомлений. v1: только Telegram-бот; токен — в зашифрованном payload."""

    __tablename__ = "ih_notification_channels"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=new_id)
    space_id: Mapped[UUID] = mapped_column(ForeignKey("ih_spaces.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(32), default="telegram")
    title: Mapped[str] = mapped_column(String(200))
    config: Mapped[dict[str, Any]] = mapped_column(JSONB)  # {"chat_id": -100..., "thread_id": 12}
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (CheckConstraint("kind IN ('telegram')", name="kind_valid"),)


class NotificationRuleRecord(IdentityHubBase):
    """Правило маршрутизации событий в канал. Редактируется из админки."""

    __tablename__ = "ih_notification_rules"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=new_id)
    space_id: Mapped[UUID] = mapped_column(ForeignKey("ih_spaces.id", ondelete="CASCADE"))
    channel_id: Mapped[UUID] = mapped_column(ForeignKey("ih_notification_channels.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    enabled: Mapped[bool] = mapped_column(default=True)

    event_types: Mapped[list[str]] = mapped_column(ARRAY(String(64)))
    min_severity: Mapped[str] = mapped_column(String(16), default=Severity.WARNING)
    # декларативный фильтр, без кода: {"service": ["telegram"], "data.to": ["frozen", "banned"],
    #                                   "labels": {"farm": "3"}}
    filter: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    digest_seconds: Mapped[int | None] = mapped_column(Integer)  # None — сразу, иначе окно сводки
    template: Mapped[str | None] = mapped_column(Text)  # None — шаблон по умолчанию для типа

    created_by: Mapped[str] = mapped_column(String(256))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        _enum_check("min_severity", Severity),
        CheckConstraint("digest_seconds IS NULL OR digest_seconds BETWEEN 10 AND 86400", name="digest_range"),
        Index("ix_ih_notification_rules_space", "space_id", postgresql_where=text("enabled")),
    )


class NotificationRecord(IdentityHubBase):
    """Исходящее уведомление. Для дайджеста — копит события до scheduled_at."""

    __tablename__ = "ih_notifications"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    rule_id: Mapped[UUID] = mapped_column(ForeignKey("ih_notification_rules.id", ondelete="CASCADE"))
    channel_id: Mapped[UUID]
    digest_key: Mapped[str | None] = mapped_column(String(128))  # rule_id + начало окна
    event_ids: Mapped[list[UUID]] = mapped_column(ARRAY(PGUUID(as_uuid=True)))
    status: Mapped[str] = mapped_column(String(16), default=DeliveryStatus.PENDING)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # когда отправлять
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        _enum_check("status", DeliveryStatus),
        # одно открытое окно дайджеста на правило
        Index(
            "uq_ih_notifications_open_digest",
            "digest_key",
            unique=True,
            postgresql_where=text("digest_key IS NOT NULL AND status = 'pending'"),
        ),
        Index(
            "ix_ih_notifications_due",
            "scheduled_at",
            postgresql_where=text("status IN ('pending', 'failed')"),
        ),
    )
