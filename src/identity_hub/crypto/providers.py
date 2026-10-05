"""Источники мастер-ключей (KEK) — реализации `SecretsProvider` (ТЗ S-4).

Формат ключей в переменной окружения и файле: `key_id:base64` через запятую или с новой строки;
первый ключ — активный (им шифруются новые записи), остальные — для расшифровки после ротации.
Тем же форматом задаётся необязательное кольцо ключей отпечатков (D-207): переменная
`IDENTITY_HUB_FINGERPRINT_KEYS` или `fingerprint_path` у файлового провайдера. Без него ключи
отпечатков выводятся из KEK (`identity_hub.crypto.fingerprint`).

Сообщения об ошибках не содержат ни значения ключа, ни `key_id`: при перепутанном порядке
(`base64:key_id`) на месте идентификатора стоит сам ключ. Ошибка называет номер записи.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
import secrets
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import NoReturn, Self

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
_KEY_ID = re.compile(r"[A-Za-z0-9_.-]{1,64}")  # колонка key_id — String(64)
_KEY_ID_RULE = "идентификатор — от 1 до 64 символов из A-Z, a-z, 0-9, «_», «.», «-»"


def _valid_key_id(key_id: object) -> bool:
    return isinstance(key_id, str) and _KEY_ID.fullmatch(key_id) is not None


def generate_key(key_id: str = "k1") -> str:
    """Новый KEK в формате `key_id:base64` (для конфигурации)."""
    return f"{key_id}:{base64.b64encode(secrets.token_bytes(KEY_BYTES)).decode()}"


def parse_keys(spec: str) -> tuple[str, dict[str, bytes]]:
    """`k2:...,k1:...` → (активный key_id, все ключи). BOM в начале файла игнорируется."""
    keys: dict[str, bytes] = {}
    position = 0
    for chunk in spec.replace("\ufeff", "").replace("\n", ",").split(","):
        chunk = chunk.strip()
        if not chunk or chunk.startswith("#"):
            continue
        position += 1
        label = f"мастер-ключ №{position}"
        key_id, sep, encoded = chunk.partition(":")
        if not sep or not key_id:
            raise ValidationFailed(f"{label}: ожидается формат key_id:base64")
        if not _valid_key_id(key_id):
            raise ValidationFailed(f"{label}: {_KEY_ID_RULE}; ожидается формат key_id:base64")
        raw = _decode(encoded)
        if raw is None:
            raise ValidationFailed(f"{label}: некорректный base64")
        if len(raw) != KEY_BYTES:
            raise ValidationFailed(f"{label}: нужно {KEY_BYTES} байта, получено {len(raw)}")
        if len(set(raw)) == 1:
            raise ValidationFailed(f"{label}: ключ состоит из одинаковых байтов; создайте его `generate_key`")
        if key_id in keys:
            raise ValidationFailed(f"{label}: идентификатор уже использован другим ключом")
        keys[key_id] = raw
    if not keys:
        raise ValidationFailed("мастер-ключи не заданы")
    return next(iter(keys)), keys


def _decode(encoded: str) -> bytes | None:
    try:
        return base64.b64decode(encoded, validate=True)
    except binascii.Error:
        return None


class StaticSecretsProvider:
    """Ключи, переданные напрямую (тесты, собственная загрузка из Vault/OpenBao).

    `fingerprint_keys` и `fingerprint_active` — отдельное кольцо ключей отпечатков (D-207); без
    него ключи отпечатков выводятся из KEK.
    """

    def __init__(
        self,
        keys: Mapping[str, bytes],
        active: str,
        *,
        fingerprint_keys: Mapping[str, bytes] | None = None,
        fingerprint_active: str | None = None,
    ) -> None:
        self._set(keys, active)
        self._set_fingerprint(fingerprint_keys, fingerprint_active)

    def _set_fingerprint(self, keys: Mapping[str, bytes] | None, active: str | None) -> None:
        """Проверить и установить кольцо отпечатков; при ошибке прежнее кольцо остаётся."""
        if not keys:
            if active is not None:
                raise ValidationFailed("активный ключ отпечатков задан без кольца ключей")
            self._fingerprint_keys: Mapping[str, bytes] | None = None
            self._fingerprint_active: str | None = None
            return
        for position, (key_id, raw) in enumerate(keys.items(), start=1):
            if not _valid_key_id(key_id):
                raise ValidationFailed(f"ключ отпечатков №{position}: {_KEY_ID_RULE}")
            if len(raw) != KEY_BYTES:
                raise ValidationFailed(f"ключ отпечатков №{position}: нужно {KEY_BYTES} байта")
        active = active if active is not None else next(iter(keys))
        if active not in keys:
            raise ValidationFailed("активный ключ отпечатков отсутствует среди переданных")
        if set(keys.values()) & set(self._keys.values()):
            raise ValidationFailed("ключ отпечатков совпадает с мастер-ключом шифрования")
        self._fingerprint_keys = MappingProxyType(dict(keys))
        self._fingerprint_active = active

    def fingerprint_keys(self) -> Mapping[str, bytes] | None:
        """Явное кольцо ключей отпечатков; `None` — ключи выводятся из KEK."""
        return self._fingerprint_keys

    def active_fingerprint_key_id(self) -> str | None:
        return self._fingerprint_active

    def _set(self, keys: Mapping[str, bytes], active: str) -> None:
        for position, (key_id, raw) in enumerate(keys.items(), start=1):
            if not _valid_key_id(key_id):
                raise ValidationFailed(f"мастер-ключ №{position}: {_KEY_ID_RULE}")
            if len(raw) != KEY_BYTES:
                raise ValidationFailed(f"мастер-ключ {key_id}: нужно {KEY_BYTES} байта")
        if active not in keys:
            raise ValidationFailed("активный мастер-ключ отсутствует среди переданных")
        # копия и представление только для чтения: ни вызывающий, ни получатель keys() набор не изменят
        self._keys: Mapping[str, bytes] = MappingProxyType(dict(keys))
        self._active = active

    def keys(self) -> Mapping[str, bytes]:
        return self._keys

    def active_key_id(self) -> str:
        return self._active

    def __repr__(self) -> str:
        extra = ""
        if self._fingerprint_keys is not None:
            extra = f", fingerprint_active={self._fingerprint_active!r}"
        return f"{type(self).__name__}(active={self._active!r}, keys={sorted(self._keys)}{extra})"

    def __reduce__(self) -> NoReturn:
        raise TypeError("провайдер мастер-ключей нельзя сериализовать pickle")

    def __copy__(self) -> Self:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> Self:
        return self


class EnvSecretsProvider(StaticSecretsProvider):
    """Ключи из переменной окружения (по умолчанию `IDENTITY_HUB_KEK`)."""

    def __init__(
        self,
        variable: str = "IDENTITY_HUB_KEK",
        fingerprint_variable: str = "IDENTITY_HUB_FINGERPRINT_KEYS",
    ) -> None:
        value = os.environ.get(variable)
        if not value:
            raise ValidationFailed(f"переменная окружения {variable} не задана")
        active, keys = parse_keys(value)
        fingerprint = os.environ.get(fingerprint_variable)
        fp_active, fp_keys = parse_keys(fingerprint) if fingerprint else (None, None)
        super().__init__(keys, active, fingerprint_keys=fp_keys, fingerprint_active=fp_active)


class FileSecretsProvider(StaticSecretsProvider):
    """Ключи из файла (Docker/K8s secrets). `reload()` перечитывает файл без перезапуска процесса."""

    def __init__(self, path: str | Path, *, fingerprint_path: str | Path | None = None) -> None:
        self._path = Path(path)
        self._fingerprint_path = Path(fingerprint_path) if fingerprint_path is not None else None
        self.reload()

    def reload(self) -> None:
        """Перечитать файл: первый ключ становится активным. При ошибке остаются прежние ключи."""
        text = self._read()
        if text is None:  # вне except: в цепочке исключения нет пути и текста ошибки ОС
            raise ValidationFailed("файл мастер-ключей не удалось прочитать")
        active, keys = parse_keys(text)
        fp_active: str | None = None
        fp_keys: dict[str, bytes] | None = None
        if self._fingerprint_path is not None:
            fp_text = self._read(self._fingerprint_path)
            if fp_text is None:
                raise ValidationFailed("файл ключей отпечатков не удалось прочитать")
            fp_active, fp_keys = parse_keys(fp_text)
        previous = (self.__dict__.get("_keys"), self.__dict__.get("_active"))
        self._set(keys, active)
        try:
            self._set_fingerprint(fp_keys, fp_active)
        except ValidationFailed:
            if previous[0] is not None:
                self._set(previous[0], str(previous[1]))
            raise

    def _read(self, path: Path | None = None) -> str | None:
        try:
            return (path or self._path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
