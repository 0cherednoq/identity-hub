"""Кольцо ключей отпечатков: HMAC-SHA-256 equivalence tag (IMP-04, D-207).

Отпечаток различает значения с секретной частью (логин прокси, позже — пароль в команде), не
храня её: `tag = HMAC(ключ отпечатков, значение)`. Ключ отпечатков — не KEK шифрования. Провайдер
может отдать отдельное кольцо (`fingerprint_keys()`, `active_fingerprint_key_id()`); без него
ключ выводится из каждого KEK через HKDF-SHA-256 с отдельной меткой под тем же `key_id`, поэтому
исторические KEK дают исторические ключи отпечатков.

Ни ключи, ни теги не попадают в repr, события и логи.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from typing import TYPE_CHECKING, NoReturn

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from identity_hub.errors import SecretConfigurationError, ValidationFailed

if TYPE_CHECKING:
    from identity_hub.protocols import SecretsProvider

__all__ = ["FINGERPRINT_INFO", "FingerprintKeyring", "derive_fingerprint_key"]

FINGERPRINT_INFO = b"identity-hub:fingerprint:v1"


def derive_fingerprint_key(kek: bytes) -> bytes:
    """Ключ отпечатков, выведенный из KEK: HKDF-SHA-256 с меткой `FINGERPRINT_INFO`."""
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=FINGERPRINT_INFO).derive(kek)


class FingerprintKeyring:
    """Ключи отпечатков провайдера; читает провайдер при каждом вызове (`reload()` файла виден сразу)."""

    __slots__ = ("_derived", "_secrets")

    def __init__(self, secrets: SecretsProvider) -> None:
        self._secrets = secrets
        self._derived: dict[tuple[str, bytes], bytes] = {}

    def _explicit(self) -> tuple[str, Mapping[str, bytes]] | None:
        keys_of = getattr(self._secrets, "fingerprint_keys", None)
        keys = keys_of() if callable(keys_of) else None
        if not keys:
            return None
        active = self._secrets.active_fingerprint_key_id()  # type: ignore[attr-defined]
        if active not in keys:
            raise ValidationFailed("активный ключ отпечатков отсутствует среди переданных")
        keks = set(self._secrets.keys().values())
        if any(key in keks for key in keys.values()):
            raise ValidationFailed("ключ отпечатков совпадает с мастер-ключом шифрования")
        return active, keys

    def keys(self) -> Mapping[str, bytes]:
        """Все ключи отпечатков по key_id: активный и исторические."""
        explicit = self._explicit()
        if explicit is not None:
            return explicit[1]
        result: dict[str, bytes] = {}
        for key_id, kek in self._secrets.keys().items():
            cached = self._derived.get((key_id, kek))
            if cached is None:
                cached = self._derived[(key_id, kek)] = derive_fingerprint_key(kek)
            result[key_id] = cached
        return result

    @property
    def active_key_id(self) -> str:
        explicit = self._explicit()
        return explicit[0] if explicit is not None else self._secrets.active_key_id()

    def tag(self, value: str, key_id: str | None = None) -> str:
        """HMAC-SHA-256 значения ключом `key_id` (по умолчанию — активным), hex."""
        keys = self.keys()
        kid = key_id or self.active_key_id
        key = keys.get(kid)
        if key is None:
            raise SecretConfigurationError("нет ключа отпечатков, которым подписана запись")
        return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()

    def tags(self, value: str) -> dict[str, str]:
        """Теги значения всеми ключами кольца: для поиска, пока не все записи пересчитаны."""
        encoded = value.encode("utf-8")
        return {kid: hmac.new(key, encoded, hashlib.sha256).hexdigest() for kid, key in self.keys().items()}

    def __repr__(self) -> str:
        return f"{type(self).__name__}(active={self.active_key_id!r})"

    def __reduce__(self) -> NoReturn:
        raise TypeError("кольцо ключей отпечатков нельзя сериализовать pickle")
