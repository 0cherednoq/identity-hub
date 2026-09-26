"""Источники мастер-ключей (KEK) — реализации `SecretsProvider` (ТЗ S-4).

Формат ключей в переменной окружения и файле: `key_id:base64` через запятую или с новой строки;
первый ключ — активный (им шифруются новые записи), остальные — для расшифровки после ротации.
"""

from __future__ import annotations

import base64
import binascii
import os
import secrets
from collections.abc import Mapping
from pathlib import Path

from identity_hub.errors import ValidationFailed

__all__ = [
    "KEY_BYTES",
    "EnvSecretsProvider",
    "FileSecretsProvider",
    "StaticSecretsProvider",
    "generate_key",
    "parse_keys",
]

KEY_BYTES = 32  # AES-256


def generate_key(key_id: str = "k1") -> str:
    """Новый KEK в формате `key_id:base64` (для конфигурации)."""
    return f"{key_id}:{base64.b64encode(secrets.token_bytes(KEY_BYTES)).decode()}"


def parse_keys(spec: str) -> tuple[str, dict[str, bytes]]:
    """`k2:...,k1:...` → (активный key_id, все ключи)."""
    keys: dict[str, bytes] = {}
    order: list[str] = []
    for chunk in spec.replace("\n", ",").split(","):
        chunk = chunk.strip()
        if not chunk or chunk.startswith("#"):
            continue
        key_id, sep, encoded = chunk.partition(":")
        if not sep or not key_id:
            raise ValidationFailed("мастер-ключ: ожидается формат key_id:base64")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except binascii.Error:
            raise ValidationFailed(f"мастер-ключ {key_id}: некорректный base64") from None
        if len(raw) != KEY_BYTES:
            raise ValidationFailed(f"мастер-ключ {key_id}: нужно {KEY_BYTES} байта, получено {len(raw)}")
        if key_id in keys:
            raise ValidationFailed(f"мастер-ключ {key_id} указан дважды")
        keys[key_id] = raw
        order.append(key_id)
    if not order:
        raise ValidationFailed("мастер-ключи не заданы")
    return order[0], keys


class StaticSecretsProvider:
    """Ключи, переданные напрямую (тесты, собственная загрузка из Vault/OpenBao)."""

    def __init__(self, keys: Mapping[str, bytes], active: str) -> None:
        if active not in keys:
            raise ValidationFailed(f"активный ключ {active} отсутствует")
        for key_id, raw in keys.items():
            if len(raw) != KEY_BYTES:
                raise ValidationFailed(f"мастер-ключ {key_id}: нужно {KEY_BYTES} байта")
        self._keys = dict(keys)
        self._active = active

    def keys(self) -> Mapping[str, bytes]:
        return self._keys

    def active_key_id(self) -> str:
        return self._active

    def __repr__(self) -> str:
        return f"{type(self).__name__}(active={self._active!r}, keys={sorted(self._keys)})"


class EnvSecretsProvider(StaticSecretsProvider):
    """Ключи из переменной окружения (по умолчанию `IDENTITY_HUB_KEK`)."""

    def __init__(self, variable: str = "IDENTITY_HUB_KEK") -> None:
        value = os.environ.get(variable)
        if not value:
            raise ValidationFailed(f"переменная окружения {variable} не задана")
        active, keys = parse_keys(value)
        super().__init__(keys, active)


class FileSecretsProvider(StaticSecretsProvider):
    """Ключи из файла (Docker/K8s secrets)."""

    def __init__(self, path: str | Path) -> None:
        active, keys = parse_keys(Path(path).read_text(encoding="utf-8"))
        super().__init__(keys, active)
