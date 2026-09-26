"""P1.4: декларация сервиса и её валидация (FR-1.2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import BaseModel

from identity_hub.domain.regions import Observation
from identity_hub.domain.verification import VerificationSpec, VerificationTier
from identity_hub.enums import Standing
from identity_hub.errors import ValidationFailed
from identity_hub.protocols import Authenticator, Blame, Fault, LoginChallenge
from identity_hub.service import PROBE_OPERATION, OperationRule, Pacing, Quota, Service
from tests.fixtures.services import FAKE_EXCHANGE, FAKE_TG

pytestmark = pytest.mark.unit


def test_fixture_services_are_valid() -> None:
    assert FAKE_TG.rule("send_message").standing == {Standing.GOOD}
    assert FAKE_EXCHANGE.factor_names == {"email", "phone", "kyc_id"}
    assert FAKE_EXCHANGE.tiers_by_name["full"].factors == {"email", "phone", "kyc_id"}
    assert FAKE_TG.standing_for_code("spam_restricted") == Standing.LIMITED
    assert FAKE_TG.standing_for_code("nope") is None


def test_probe_operation_added_automatically() -> None:
    rule = FAKE_TG.rule(PROBE_OPERATION)
    assert rule.needs_auth is False
    assert rule.standing == set(Standing)


def test_unknown_operation() -> None:
    with pytest.raises(ValidationFailed):
        FAKE_TG.rule("fly")


class Profile(BaseModel):
    country: str | None = None


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"name": "Bad-Name"}, "name"),
        ({"profile": Profile, "filterable_profile": frozenset({"age"})}, "filterable_profile"),
        ({"standing_codes": {"x": "weird"}}, "standing_codes"),
        ({"verifications": (VerificationSpec("a"), VerificationSpec("a"))}, "повторяющиеся"),
        ({"verifications": (VerificationSpec("a", requires=frozenset({"b"})),)}, "зависимости"),
        (
            {
                "verifications": (
                    VerificationSpec("a", requires=frozenset({"b"})),
                    VerificationSpec("b", requires=frozenset({"a"})),
                )
            },
            "циклическая",
        ),
        ({"tiers": (VerificationTier("t", frozenset({"x"})),)}, "tier"),
        ({"operations": {"op": OperationRule(verified=frozenset({"x"}))}}, "факторы"),
        ({"operations": {"op": OperationRule(tiers=frozenset({"x"}))}}, "уровни"),
        ({"normalizers": {"lifecycle": lambda c, o: o}}, "не синхронизируется"),
        ({"normalizers": {"weird": lambda c, o: o}}, "неизвестный регион"),
    ],
)
def test_invalid_declarations(kwargs: dict[str, object], fragment: str) -> None:
    params: dict[str, object] = {"name": "svc", "title": "Svc", **kwargs}
    with pytest.raises(ValidationFailed) as exc:
        Service(**params)  # type: ignore[arg-type]
    assert any(fragment in e for e in exc.value.errors)


def test_region_spec_uses_service_normalizer() -> None:
    svc = Service(
        name="svc",
        title="Svc",
        normalizers={"standing": lambda cur, obs: Standing.LIMITED if obs == Standing.FROZEN else obs},
    )
    spec = svc.region_spec("standing")
    assert spec.normalize is not None
    assert spec.normalize(Standing.GOOD, Standing.FROZEN) == Standing.LIMITED
    verification = svc.region_spec("verification")
    assert verification.normalize is not None
    assert verification.normalize("verified", "missing") == "revoked"
    assert isinstance(Observation(Standing.GOOD, datetime.now(UTC)), Observation)


def test_quota_windows_follow_date_bin() -> None:
    origin = datetime(2026, 1, 1, 3, tzinfo=UTC)  # полночь UTC+3
    daily = Quota(10, timedelta(days=1), origin=origin)
    assert daily.window_start(datetime(2026, 3, 5, 2, 59, tzinfo=UTC)) == datetime(2026, 3, 4, 3, tzinfo=UTC)
    assert daily.window_start(datetime(2026, 3, 5, 3, 0, tzinfo=UTC)) == datetime(2026, 3, 5, 3, tzinfo=UTC)
    hourly = Quota(5, timedelta(hours=1))
    assert hourly.window_seconds == 3600


@pytest.mark.parametrize(
    "factory",
    [
        lambda: Quota(0, timedelta(hours=1)),
        lambda: Quota(1, timedelta(milliseconds=10)),
        lambda: Quota(1, timedelta(hours=1), origin=datetime(2026, 1, 1)),
        lambda: Pacing(Quota(1, timedelta(hours=1)), (10, 5)),
        lambda: OperationRule(standing=frozenset({"weird"})),
        lambda: OperationRule(quotas=(Quota(1, timedelta(hours=1)), Quota(2, timedelta(hours=1)))),
    ],
)
def test_invalid_limits(factory: object) -> None:
    with pytest.raises(ValueError):
        factory()  # type: ignore[operator]


def test_protocol_helpers() -> None:
    class Auth:
        async def login(self, ctx: object) -> BaseModel:
            raise LoginChallenge("sms_code", "код из SMS", {"phone_code_hash": "h"}, timedelta(minutes=5))

        async def refresh(self, credentials: object, session: BaseModel) -> BaseModel | None:
            return None

    assert isinstance(Auth(), Authenticator)
    fault = Fault(Blame.IDENTITY, retry_after=timedelta(seconds=30))
    assert fault.weight == 1.0
