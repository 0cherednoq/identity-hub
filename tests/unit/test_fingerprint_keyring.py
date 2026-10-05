"""IMP-04 (I1.4a): кольцо ключей отпечатков — отдельно от KEK, ротация, без ключей в repr (D-207)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import pickle
from pathlib import Path

import pytest

from identity_hub import SecretConfigurationError, StaticSecretsProvider, ValidationFailed
from identity_hub.crypto import EnvSecretsProvider, FileSecretsProvider
from identity_hub.crypto.fingerprint import FingerprintKeyring, derive_fingerprint_key

pytestmark = pytest.mark.unit

KEK1, KEK2 = bytes(range(32)), bytes(range(1, 33))
FP1, FP2 = bytes(range(2, 34)), bytes(range(3, 35))


def _line(key_id: str, raw: bytes) -> str:
    return f"{key_id}:{base64.b64encode(raw).decode()}"


def test_derived_key_is_not_the_kek() -> None:
    keyring = FingerprintKeyring(StaticSecretsProvider({"k1": KEK1}, "k1"))

    key = keyring.keys()["k1"]

    assert key != KEK1 and key == derive_fingerprint_key(KEK1)
    assert keyring.tag("value") != hmac.new(KEK1, b"value", hashlib.sha256).hexdigest()
    assert keyring.tag("value") == hmac.new(key, b"value", hashlib.sha256).hexdigest()


def test_same_value_same_tag_other_value_other_tag() -> None:
    keyring = FingerprintKeyring(StaticSecretsProvider({"k1": KEK1}, "k1"))

    assert keyring.tag("proxy:v1|socks5|h|1|alice") == keyring.tag("proxy:v1|socks5|h|1|alice")
    assert keyring.tag("proxy:v1|socks5|h|1|alice") != keyring.tag("proxy:v1|socks5|h|1|bob")


def test_explicit_keyring_overrides_derivation_and_rotates() -> None:
    old = FingerprintKeyring(StaticSecretsProvider({"k1": KEK1}, "k1", fingerprint_keys={"f1": FP1}))
    new = FingerprintKeyring(
        StaticSecretsProvider(
            {"k1": KEK1}, "k1", fingerprint_keys={"f2": FP2, "f1": FP1}, fingerprint_active="f2"
        )
    )

    assert old.active_key_id == "f1" and new.active_key_id == "f2"
    assert new.tags("v")["f1"] == old.tag("v")  # исторический ключ даёт прежний тег
    assert new.tag("v") != old.tag("v")


def test_fingerprint_key_equal_to_kek_is_rejected() -> None:
    with pytest.raises(ValidationFailed, match="совпадает с мастер-ключом"):
        StaticSecretsProvider({"k1": KEK1}, "k1", fingerprint_keys={"f1": KEK1})


def test_missing_key_is_configuration_error() -> None:
    keyring = FingerprintKeyring(StaticSecretsProvider({"k2": KEK2}, "k2"))

    with pytest.raises(SecretConfigurationError) as caught:
        keyring.tag("v", key_id="k1")
    assert "k1" not in str(caught.value)


def test_no_key_material_in_repr_or_pickle() -> None:
    provider = StaticSecretsProvider({"k1": KEK1}, "k1", fingerprint_keys={"f1": FP1})
    keyring = FingerprintKeyring(provider)
    encoded = (base64.b64encode(FP1).decode(), FP1.hex(), repr(FP1))

    for text in (repr(provider), repr(keyring)):
        assert not any(part in text for part in encoded)
    with pytest.raises(TypeError):
        pickle.dumps(keyring)


def test_env_and_file_providers_read_fingerprint_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("IDENTITY_HUB_KEK", _line("k1", KEK1))
    monkeypatch.setenv("IDENTITY_HUB_FINGERPRINT_KEYS", f"{_line('f2', FP2)},{_line('f1', FP1)}")
    env = FingerprintKeyring(EnvSecretsProvider())
    assert env.active_key_id == "f2" and set(env.keys()) == {"f2", "f1"}

    keks, fps = tmp_path / "keks", tmp_path / "fps"
    keks.write_text(_line("k1", KEK1), encoding="utf-8")
    fps.write_text(_line("f1", FP1), encoding="utf-8")
    provider = FileSecretsProvider(keks, fingerprint_path=fps)
    assert FingerprintKeyring(provider).active_key_id == "f1"
    fps.write_text(_line("bad", KEK1), encoding="utf-8")
    with pytest.raises(ValidationFailed):
        provider.reload()
    assert FingerprintKeyring(provider).active_key_id == "f1"  # прежнее кольцо осталось
