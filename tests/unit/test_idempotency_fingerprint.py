"""F1.1 (IMP-03): канонический fingerprint команды."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from enum import StrEnum
from uuid import UUID

import pytest
from pydantic import SecretStr

from identity_hub import StaticSecretsProvider
from identity_hub.crypto.fingerprint import FingerprintKeyring
from identity_hub.crypto.secret_value import SecretValue
from identity_hub.domain.idempotency import (
    OMITTED,
    KeyedFingerprint,
    canonical_json,
    fingerprint,
    keyed_fingerprint,
)

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


KEK, FP1, FP2 = bytes(range(32)), bytes(range(1, 33)), bytes(range(2, 34))


def _keyed(password: str, keyring: FingerprintKeyring) -> KeyedFingerprint:
    return keyed_fingerprint(
        "replace_credentials",
        {"identity_id": "i-1"},
        {"value": {"password": password}},
        tags=keyring.tags,
        active_key_id=keyring.active_key_id,
    )


def test_keyed_fingerprint_hides_secret_and_tracks_keys() -> None:
    """I1.4b (D-208): секрет — HMAC-тегом; отпечатки историческими ключами для повтора после ротации."""
    old = FingerprintKeyring(StaticSecretsProvider({"k1": KEK}, "k1", fingerprint_keys={"f1": FP1}))
    new = FingerprintKeyring(
        StaticSecretsProvider(
            {"k1": KEK}, "k1", fingerprint_keys={"f2": FP2, "f1": FP1}, fingerprint_active="f2"
        )
    )

    first, again, other = _keyed("pw-1", old), _keyed("pw-1", old), _keyed("pw-2", old)
    rotated = _keyed("pw-1", new)

    assert first == again and first.fingerprint != other.fingerprint
    assert (first.key_id, rotated.key_id) == ("f1", "f2")
    assert rotated.for_key("f1") == first.fingerprint
    assert rotated.for_key("gone") is None
    assert "pw-1" not in repr(rotated) and first.fingerprint not in repr(rotated)
