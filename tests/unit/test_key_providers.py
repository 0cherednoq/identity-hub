"""F1.7: провайдеры мастер-ключей (R2-S13)."""

from __future__ import annotations

import base64
import copy
import pickle
from pathlib import Path

import pytest

from identity_hub.crypto import (
    Envelope,
    EnvSecretsProvider,
    FileSecretsProvider,
    StaticSecretsProvider,
    generate_key,
    parse_keys,
)
from identity_hub.errors import ValidationFailed

pytestmark = pytest.mark.unit

RAW = bytes(range(32))
B64 = base64.b64encode(RAW).decode()


@pytest.mark.parametrize(
    "spec",
    [
        f"{B64}:k1",  # перепутан порядок: материал ключа стоит на месте key_id
        f"k1:{B64[:-4]}",  # короткий ключ
        f"k1:{B64}!",  # не base64
        f"k1:{B64},k1:{B64}",  # повтор
        f"{B64}",  # без key_id
    ],
    ids=["swapped", "short", "not-base64", "duplicate", "no-id"],
)
def test_parse_error_never_contains_input(spec: str) -> None:
    """R2-S13: при ошибке формата материал ключа не попадает в сообщение и stderr CLI."""
    with pytest.raises(ValidationFailed) as exc:
        parse_keys(spec)
    message = str(exc.value)
    assert B64 not in message and B64[:16] not in message
    assert exc.value.__cause__ is None and exc.value.__context__ is None


def test_bom_is_stripped(tmp_path: Path) -> None:
    path = tmp_path / "kek"
    path.write_text(f"k1:{B64}\n", encoding="utf-8-sig")  # так сохраняет Блокнот Windows

    provider = FileSecretsProvider(path)

    assert provider.active_key_id() == "k1" and list(provider.keys()) == ["k1"]
    assert parse_keys(f"﻿k1:{B64}")[0] == "k1"


@pytest.mark.parametrize("key_id", ["k" * 65, "айди", "k 1", "k/1", "k1\x00", "a=b"])
def test_key_id_alphabet_and_length(key_id: str) -> None:
    """`key_id` хранится в колонке `String(64)`: длина и алфавит проверяются при загрузке."""
    with pytest.raises(ValidationFailed) as exc:
        parse_keys(f"{key_id}:{B64}")
    assert key_id not in str(exc.value)
    with pytest.raises(ValidationFailed):
        StaticSecretsProvider({key_id: RAW}, key_id)


def test_valid_key_ids_are_accepted() -> None:
    for key_id in ("k1", "prod-2026.10_a", "K" * 64):
        assert parse_keys(f"{key_id}:{B64}")[0] == key_id


@pytest.mark.parametrize("byte", [0, 1, 255])
def test_key_of_identical_bytes_is_rejected(byte: int) -> None:
    weak = base64.b64encode(bytes([byte]) * 32).decode()
    with pytest.raises(ValidationFailed) as exc:
        parse_keys(f"k1:{weak}")
    assert weak not in str(exc.value)


def test_keys_mapping_is_read_only() -> None:
    source = {"k1": RAW}
    provider = StaticSecretsProvider(source, "k1")
    keys = provider.keys()

    with pytest.raises(TypeError):
        keys["k2"] = RAW  # type: ignore[index]
    with pytest.raises(TypeError):
        del keys["k1"]  # type: ignore[attr-defined]
    source["k2"] = b"\x05" * 32  # исходный словарь вызывающего на провайдер не влияет
    assert list(provider.keys()) == ["k1"]


def test_provider_cannot_be_pickled_and_copy_does_not_duplicate_keys() -> None:
    provider = StaticSecretsProvider({"k1": RAW}, "k1")
    with pytest.raises(TypeError):
        pickle.dumps(provider)
    assert copy.copy(provider) is provider and copy.deepcopy(provider) is provider
    assert B64 not in repr(provider) and repr(RAW) not in repr(provider)


def test_file_provider_reload_picks_up_rotation(tmp_path: Path) -> None:
    path = tmp_path / "kek"
    old = generate_key("k1")
    path.write_text(old, encoding="utf-8")
    provider = FileSecretsProvider(path)
    envelope = Envelope(provider)
    sealed = envelope.seal(b"secret", b"aad")
    assert sealed.key_id == "k1"

    path.write_text(f"{generate_key('k2')}\n{old}\n", encoding="utf-8")
    assert provider.active_key_id() == "k1"  # до reload() конфигурация прежняя
    provider.reload()

    assert provider.active_key_id() == "k2" and list(provider.keys()) == ["k2", "k1"]
    assert envelope.seal(b"new", b"aad").key_id == "k2"
    assert envelope.open(sealed, b"aad") == b"secret"


def test_failed_reload_keeps_previous_keys(tmp_path: Path) -> None:
    path = tmp_path / "kek"
    path.write_text(generate_key("k1"), encoding="utf-8")
    provider = FileSecretsProvider(path)
    before = dict(provider.keys())

    path.write_text(f"{B64}:k2", encoding="utf-8")
    with pytest.raises(ValidationFailed):
        provider.reload()
    path.unlink()
    with pytest.raises(ValidationFailed) as exc:
        provider.reload()

    assert str(path) not in str(exc.value)
    assert dict(provider.keys()) == before and provider.active_key_id() == "k1"


def test_env_provider_error_does_not_echo_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IH_TEST_KEK", f"{B64}:k1")
    with pytest.raises(ValidationFailed) as exc:
        EnvSecretsProvider("IH_TEST_KEK")
    assert B64 not in str(exc.value)
