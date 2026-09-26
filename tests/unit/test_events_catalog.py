"""P1.8: каталог событий, маски, очистка секретов."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import SecretStr

from identity_hub.domain.region_specs import AUTH, AVAILABILITY, LIFECYCLE, PROXY_HEALTH, STANDING
from identity_hub.domain.verification import VERIFICATION
from identity_hub.enums import Severity, Standing
from identity_hub.events import (
    CATALOG,
    Event,
    EventDraft,
    SecretLeak,
    matches,
    matches_any,
    scrub,
    severity_at_least,
)
from tests.fixtures.services import TgCredentials

pytestmark = pytest.mark.unit


def test_catalog_covers_spec_and_region_events() -> None:
    assert len([s for s in CATALOG.values() if s.external]) == 30
    for spec in (LIFECYCLE, STANDING, AUTH, AVAILABILITY, PROXY_HEALTH, VERIFICATION):
        assert spec.event_type in CATALOG
    assert "identity.state.drift" in CATALOG
    assert not CATALOG["lease.acquired"].external
    assert not CATALOG["session.saved"].external


def test_draft_requires_catalog_type() -> None:
    with pytest.raises(ValueError):
        EventDraft(type="identity.exploded", actor="w")
    draft = EventDraft(type="pool.capacity.empty", actor="hub")
    assert draft.effective_severity is Severity.CRITICAL
    assert EventDraft(
        type="identity.standing.changed", actor="w", severity=Severity.WARNING
    ).effective_severity == (Severity.WARNING)


def test_masks() -> None:
    assert matches("pool.capacity.*", "pool.capacity.low")
    assert not matches("pool.capacity.*", "pool.capacityx")
    assert matches("*", "anything")
    assert matches("identity.linked", "identity.linked")
    assert not matches("identity.linked", "identity.unlinked")
    assert matches_any(["proxy.*", "identity.linked"], "proxy.status.changed")


def test_severity_ranks() -> None:
    assert severity_at_least("critical", "warning")
    assert severity_at_least("warning", "warning")
    assert not severity_at_least("info", "warning")


def test_scrub_removes_secrets_and_normalizes() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    uid = uuid4()
    data = {
        "to": Standing.BANNED,
        "at": now,
        "id": uid,
        "password": "x",
        "nested": {"token": "t", "ok": 1, "blob": b"\x00"},
        "items": [SecretStr("s"), "fine"],
        "model": TgCredentials(phone="+1"),
        "empty_secret": None,
    }
    assert scrub(data) == {
        "to": "banned",
        "at": now.isoformat(),
        "id": str(uid),
        "password": "[redacted]",
        "nested": {"token": "[redacted]", "ok": 1, "blob": "[redacted]"},
        "items": ["[redacted]", "fine"],
        "model": "[redacted]",
        "empty_secret": None,
    }


def test_scrub_strict_mode_raises() -> None:
    with pytest.raises(SecretLeak):
        scrub({"session": {"auth_key": "k"}}, strict=True)
    assert scrub({"reason": "бан ложный"}, strict=True) == {"reason": "бан ложный"}


def test_event_to_dict() -> None:
    event = Event(
        id=1,
        event_id=uuid4(),
        type="identity.registered",
        severity=Severity.INFO,
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        space="tennet",
        service="fake_tg",
        identity_id=uuid4(),
        proxy_id=None,
        lease_id=None,
        sequence=1,
        actor="w",
        data={"a": 1},
    )
    body = event.to_dict()
    assert body["space"] == "tennet"
    assert body["severity"] == "info"
    assert "id" not in body
