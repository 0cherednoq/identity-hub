"""P1.7: envelope-шифрование (ТЗ S-1..S-5)."""

from __future__ import annotations

import base64
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import SecretStr

from identity_hub.crypto import (
    DecryptionFailed,
    Envelope,
    EnvSecretsProvider,
    FileSecretsProvider,
    StaticSecretsProvider,
    associated_data,
    generate_key,
    parse_keys,
)
from identity_hub.errors import ValidationFailed
from tests.fixtures.services import TgCredentials

pytestmark = pytest.mark.unit

K1 = b"\x01" * 32
K2 = b"\x02" * 32


def envelope(active: str = "k1") -> Envelope:
    return Envelope(StaticSecretsProvider({"k1": K1, "k2": K2}, active))


def test_roundtrip_bytes_and_unique_dek() -> None:
    env = envelope()
    aad = associated_data("ih_credentials", uuid4())
    a = env.seal(b"secret", aad)
    b = env.seal(b"secret", aad)
    assert a.key_id == "k1"
    assert a.ciphertext != b.ciphertext
    assert a.wrapped_dek != b.wrapped_dek
    assert env.open(a, aad) == b"secret"


def test_blob_cannot_move_to_another_row() -> None:
    env = envelope()
    owner = uuid4()
    payload = env.seal(b"secret", associated_data("ih_sessions", owner, "default"))
    for aad in (
        associated_data("ih_sessions", uuid4(), "default"),
        associated_data("ih_sessions", owner, "backup"),
        associated_data("ih_credentials", owner, "default"),
    ):
        with pytest.raises(DecryptionFailed):
            env.open(payload, aad)


def test_tampering_detected() -> None:
    env = envelope()
    aad = associated_data("t", "o")
    payload = env.seal(b"secret", aad)
    flipped = bytes([payload.ciphertext[0] ^ 1]) + payload.ciphertext[1:]
    with pytest.raises(DecryptionFailed):
        env.open(replace(payload, ciphertext=flipped), aad)
    with pytest.raises(DecryptionFailed):
        env.open(replace(payload, key_id="k2"), aad)


def test_rotation_rewrap() -> None:
    aad = associated_data("t", "o")
    old = envelope("k1").seal(b"secret", aad)
    new_env = envelope("k2")
    assert new_env.open(old, aad) == b"secret"  # старый ключ ещё доступен
    rewrapped = new_env.rewrap(old, aad)
    assert rewrapped.key_id == "k2"
    assert rewrapped.ciphertext == old.ciphertext
    only_k2 = Envelope(StaticSecretsProvider({"k2": K2}, "k2"))
    assert only_k2.open(rewrapped, aad) == b"secret"
    with pytest.raises(DecryptionFailed, match="недоступен"):
        only_k2.open(old, aad)
    assert new_env.rewrap(rewrapped, aad) is rewrapped


def test_model_roundtrip_reveals_secrets_only_inside_ciphertext() -> None:
    env = envelope()
    aad = associated_data("ih_credentials", "o")
    creds = TgCredentials(phone="+1", password=SecretStr("hunter2"))
    payload = env.seal_model(creds, aad)
    assert b"hunter2" not in payload.ciphertext
    restored = env.open_model(TgCredentials, payload, aad)
    assert restored.password is not None
    assert restored.password.get_secret_value() == "hunter2"
    assert "hunter2" not in repr(restored)
    assert "hunter2" not in repr(payload)
    assert env.open_json(payload, aad) == {"phone": "+1", "password": "hunter2"}


def test_sibling_fields_share_dek() -> None:
    env = envelope()
    aad = associated_data("ih_challenges", "o")
    ctx = env.seal(b"context", aad)
    nonce, ct = env.seal_sibling(ctx, aad, b"12345", "answer")
    assert env.open_sibling(ctx, aad, nonce, ct, "answer") == b"12345"
    with pytest.raises(DecryptionFailed):
        env.open_sibling(ctx, aad, nonce, ct, "other")


def test_parse_keys_and_providers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = f"{generate_key('k2')},{generate_key('k1')}"
    active, keys = parse_keys(spec)
    assert active == "k2"
    assert set(keys) == {"k1", "k2"}

    monkeypatch.setenv("IH_TEST_KEK", spec)
    env_provider = EnvSecretsProvider("IH_TEST_KEK")
    assert env_provider.active_key_id() == "k2"
    assert base64.b64encode(env_provider.keys()["k1"]).decode() in spec
    assert "k1" in repr(env_provider)
    assert base64.b64encode(env_provider.keys()["k1"]).decode() not in repr(env_provider)

    path = tmp_path / "kek"
    path.write_text("# ключи\n" + spec.replace(",", "\n"), encoding="utf-8")
    assert FileSecretsProvider(path).keys() == keys


@pytest.mark.parametrize(
    "spec",
    [
        "",
        "k1",
        "k1:not-base64!",
        f"k1:{base64.b64encode(b'short').decode()}",
        f"{generate_key('a')},{generate_key('a')}",
    ],
)
def test_parse_keys_rejects(spec: str) -> None:
    with pytest.raises(ValidationFailed):
        parse_keys(spec)


def test_env_provider_requires_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IH_MISSING", raising=False)
    with pytest.raises(ValidationFailed):
        EnvSecretsProvider("IH_MISSING")
