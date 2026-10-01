"""F1.1 (IMP-03): канонический fingerprint команды."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from enum import StrEnum
from uuid import UUID

import pytest
from pydantic import SecretStr

from identity_hub.crypto.secret_value import SecretValue
from identity_hub.domain.idempotency import OMITTED, canonical_json, fingerprint

pytestmark = pytest.mark.unit


class Color(StrEnum):
    RED = "red"


def test_key_order_does_not_change_canonical_json() -> None:
    assert canonical_json({"b": 1, "a": {"y": 2, "x": 1}}) == canonical_json({"a": {"x": 1, "y": 2}, "b": 1})
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'


def test_uuid_enum_and_time_are_normalized() -> None:
    uid = UUID("00000000-0000-0000-0000-0000000000aa")
    moscow = timezone(timedelta(hours=3))
    at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert canonical_json({"id": uid, "color": Color.RED, "at": at}) == canonical_json(
        {"id": str(uid), "color": "red", "at": at.astimezone(moscow)}
    )
    with pytest.raises(TypeError):
        canonical_json({"at": datetime(2026, 1, 1)})


def test_omitted_differs_from_null() -> None:
    assert canonical_json({"a": 1, "b": OMITTED}) == canonical_json({"a": 1})
    assert canonical_json({"a": 1, "b": None}) != canonical_json({"a": 1})


def test_sets_are_ordered_and_sequences_keep_order() -> None:
    assert canonical_json({"tags": {"b", "a"}}) == canonical_json({"tags": frozenset({"a", "b"})})
    assert canonical_json([1, 2]) != canonical_json([2, 1])


@pytest.mark.parametrize("secret", [SecretStr("pw"), SecretValue("pw"), b"pw"])
def test_secrets_never_enter_fingerprint(secret: object) -> None:
    with pytest.raises(TypeError) as exc:
        fingerprint("identity.create", {"password": secret})
    assert "pw" not in str(exc.value)


def test_fingerprint_depends_on_operation_and_payload() -> None:
    base = fingerprint("operation", {"kind": "send"})
    assert base == fingerprint("operation", {"kind": "send"})
    assert len(base) == 64
    assert base != fingerprint("operation", {"kind": "read"})
    assert base != fingerprint("lease", {"kind": "send"})
