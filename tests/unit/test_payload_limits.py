"""IMP-16 (I0.3): лимиты полезной нагрузки, версия AAD, SecretValue."""

from __future__ import annotations

import copy
import json
import pickle
from typing import Any
from uuid import uuid4

import pydantic_core
import pytest

from identity_hub import IdentityCreate, IdentityHub, SecretValue, SpaceHandle, ValidationFailed
from identity_hub.crypto import Envelope, StaticSecretsProvider, associated_data
from identity_hub.crypto.envelope import MAX_SECRET_BYTES, DecryptionFailed
from identity_hub.domain.data import MAX_JSON_BYTES, MAX_JSON_DEPTH, json_depth, validate_json_payload
from identity_hub.events.catalog import SecretLeak, scrub

pytestmark = pytest.mark.anyio

KEYS = {"k1": b"\x11" * 32, "k2": b"\x22" * 32}


def _nested(depth: int) -> Any:
    value: Any = "leaf"
    for _ in range(depth):
        value = {"x": value}
    return value


# ------------------------------------------------------------------ JSON


def test_json_depth_limit() -> None:
    assert json_depth(_nested(MAX_JSON_DEPTH)) == MAX_JSON_DEPTH
    validate_json_payload(_nested(MAX_JSON_DEPTH), "profile")
    with pytest.raises(ValidationFailed, match="вложенность 17"):
        validate_json_payload(_nested(MAX_JSON_DEPTH + 1), "profile")
    assert json_depth([[], {}]) == 2 and json_depth("s") == 0


def test_json_size_boundary() -> None:
    overhead = len(pydantic_core.to_json({"k": ""}))
    exact = {"k": "a" * (MAX_JSON_BYTES - overhead)}
    assert len(pydantic_core.to_json(exact)) == MAX_JSON_BYTES
    validate_json_payload(exact, "attributes")
    with pytest.raises(ValidationFailed, match="64 KiB") as exc:
        validate_json_payload({"k": "a" * (MAX_JSON_BYTES - overhead + 1)}, "attributes")
    assert "aaaa" not in str(exc.value)  # в сообщении только размер, не содержимое


# ------------------------------------------------------------------ envelope


def test_secret_size_boundary() -> None:
    envelope = Envelope(StaticSecretsProvider(KEYS, "k1"))
    aad = associated_data("ih_credentials", uuid4())
    payload = envelope.seal(b"x" * MAX_SECRET_BYTES, aad)
    assert len(envelope.open(payload, aad)) == MAX_SECRET_BYTES
    with pytest.raises(ValidationFailed, match="512 KiB"):
        envelope.seal(b"x" * (MAX_SECRET_BYTES + 1), aad)


def test_aad_format_is_versioned() -> None:
    owner = uuid4()
    aad = associated_data("ih_sessions", owner, "web", schema_version=2)
    assert bytes(aad) == f"identity-hub:v1:ih_sessions:{owner}:web:2".encode()
    assert aad.legacy == f"ih|ih_sessions|{owner}|web".encode()


def test_legacy_payload_is_readable_and_new_writes_use_v1() -> None:
    envelope = Envelope(StaticSecretsProvider(KEYS, "k1"))
    owner = uuid4()
    aad = associated_data("ih_credentials", owner)
    legacy = envelope.seal(b'{"phone":"+1"}', aad.legacy)
    fresh = envelope.seal(b'{"phone":"+2"}', aad)

    assert envelope.open(legacy, aad) == b'{"phone":"+1"}'
    assert envelope.open(fresh, aad) == b'{"phone":"+2"}'
    with pytest.raises(DecryptionFailed):
        envelope.open(fresh, aad.legacy)  # новая запись не читается старым AAD
    with pytest.raises(DecryptionFailed):
        envelope.open(legacy, associated_data("ih_credentials", uuid4()))  # чужая строка


def test_rewrap_upgrades_legacy_to_v1() -> None:
    old = Envelope(StaticSecretsProvider(KEYS, "k1"))
    new = Envelope(StaticSecretsProvider(KEYS, "k2"))
    aad = associated_data("ih_credentials", uuid4())
    legacy = old.seal(b"secret", aad.legacy)

    rotated = new.rewrap(legacy, aad)

    assert rotated.key_id == "k2"
    assert rotated.ciphertext != legacy.ciphertext
    assert new.open(rotated, bytes(aad)) == b"secret"  # читается без legacy-формата
    assert new.rewrap(rotated, aad) is rotated  # уже в новом формате и ключе


def test_rewrap_without_upgrade_keeps_sibling_readable() -> None:
    old = Envelope(StaticSecretsProvider(KEYS, "k1"))
    new = Envelope(StaticSecretsProvider(KEYS, "k2"))
    aad = associated_data("ih_challenges", uuid4(), "c1")
    context = old.seal(b"{}", aad.legacy)
    nonce, answer = old.seal_sibling(context, aad, b"123456", "answer")

    rotated = new.rewrap(context, aad, upgrade=False)

    assert rotated.key_id == "k2" and rotated.ciphertext == context.ciphertext
    assert new.open(rotated, aad) == b"{}"
    assert new.open_sibling(rotated, aad, nonce, answer, "answer") == b"123456"


# ------------------------------------------------------------------ SecretValue


def test_secret_value_is_redacted_and_not_serializable() -> None:
    secret = SecretValue("hunter2")
    rendered = [repr(secret), str(secret), f"{secret}", f"{secret!r}", repr({"password": secret})]
    assert all("hunter2" not in text for text in rendered)
    assert secret.reveal() == "hunter2"
    assert secret == SecretValue("hunter2") and secret != SecretValue("other")
    with pytest.raises(TypeError):
        json.dumps({"password": secret})
    with pytest.raises(pydantic_core.PydanticSerializationError):
        pydantic_core.to_json({"password": secret})
    with pytest.raises(TypeError):
        pickle.dumps(secret)
    assert copy.deepcopy(secret) is secret
    with pytest.raises(TypeError):
        hash(secret)


def test_scrubber_rejects_secret_value() -> None:
    assert scrub({"value": SecretValue("x")})["value"] != "x"
    with pytest.raises(SecretLeak):
        scrub({"value": SecretValue("x")}, strict=True)


def test_envelope_seals_secret_value_explicitly() -> None:
    envelope = Envelope(StaticSecretsProvider(KEYS, "k1"))
    aad = associated_data("ih_credentials", uuid4())
    payload = envelope.seal_model({"password": SecretValue("hunter2")}, aad)
    assert envelope.open_json(payload, aad) == {"password": "hunter2"}


# ------------------------------------------------------------------ отказ до транзакции


class _NoTransaction(AssertionError):
    pass


@pytest.fixture
def space_without_tx(identity_hub: IdentityHub, monkeypatch: pytest.MonkeyPatch) -> SpaceHandle:
    space = identity_hub.space("limits")

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise _NoTransaction("транзакция не должна открываться для слишком большого ввода")

    monkeypatch.setattr(identity_hub._store, "transaction", forbidden)
    return space


@pytest.mark.parametrize(
    "spec",
    [
        {"attributes": {"deep": _nested(MAX_JSON_DEPTH + 1)}},
        {"labels": {f"k{n}": "v" * 256 for n in range(300)}},
    ],
    ids=["deep-attributes", "huge-labels"],
)
async def test_oversized_create_rejected_before_transaction(
    space_without_tx: SpaceHandle, identity_hub_service: Any, spec: dict[str, Any]
) -> None:
    with pytest.raises(ValidationFailed):
        await space_without_tx.identities.create(
            IdentityCreate(service=identity_hub_service, external_ref="+1", **spec)
        )


async def test_oversized_update_rejected_before_transaction(space_without_tx: SpaceHandle) -> None:
    with pytest.raises(ValidationFailed):
        await space_without_tx.identities.update(uuid4(), labels={f"k{n}": "v" * 256 for n in range(300)})
