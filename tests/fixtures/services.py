"""Тестовые сервисы: FAKE_TG (Telegram-подобный) и FAKE_EXCHANGE (биржа с KYC)."""

from __future__ import annotations

from datetime import date, timedelta

from pydantic import BaseModel, SecretStr

from identity_hub.domain.verification import VerificationMode, VerificationSpec, VerificationTier
from identity_hub.enums import Standing
from identity_hub.service import OperationRule, Quota, Service


class TgAttributes(BaseModel):
    device: str = "pixel"
    app_version: str = "10.0"


class TgProfile(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    country: str | None = None
    birthday: date | None = None


class TgCredentials(BaseModel):
    phone: str
    password: SecretStr | None = None


class TgSession(BaseModel):
    auth_key: SecretStr
    dc_id: int = 2


FAKE_TG = Service(
    name="fake_tg",
    title="Fake Telegram",
    url="https://telegram.org/apps",
    attributes=TgAttributes,
    profile=TgProfile,
    credentials=TgCredentials,
    session=TgSession,
    filterable_profile=frozenset({"country"}),
    searchable_profile=frozenset({"first_name", "last_name"}),
    operations={
        "read_channel": OperationRule(quotas=(Quota(100, timedelta(hours=1)),)),
        "send_message": OperationRule(
            standing=frozenset({Standing.GOOD}),
            quotas=(Quota(20, timedelta(hours=1)), Quota(100, timedelta(days=1))),
        ),
    },
    standing_codes={
        "spam_restricted": Standing.LIMITED,
        "frozen": Standing.FROZEN,
        "banned": Standing.BANNED,
    },
)


class ExCredentials(BaseModel):
    email: str
    password: SecretStr
    totp_secret: SecretStr | None = None


class ExSession(BaseModel):
    cookies: dict[str, str]


class KycEvidence(BaseModel):
    document_number: SecretStr


FAKE_EXCHANGE = Service(
    name="fake_exchange",
    title="Fake Exchange",
    credentials=ExCredentials,
    session=ExSession,
    operations={
        "view_balance": OperationRule(),
        "withdraw": OperationRule(tiers=frozenset({"full"}), quotas=(Quota(3, timedelta(days=1)),)),
    },
    verifications=(
        VerificationSpec(name="email", mode=VerificationMode.AUTO),
        VerificationSpec(name="phone", mode=VerificationMode.AUTO, pending_timeout=timedelta(minutes=10)),
        VerificationSpec(
            name="kyc_id",
            requires=frozenset({"email", "phone"}),
            valid_for=timedelta(days=365),
            max_attempts=3,
            reject_codes=frozenset({"blurry", "expired_document"}),
            evidence=KycEvidence,
        ),
    ),
    tiers=(
        VerificationTier("basic", frozenset({"email"})),
        VerificationTier("full", frozenset({"email", "phone", "kyc_id"})),
    ),
)
