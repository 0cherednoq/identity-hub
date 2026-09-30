"""Envelope encryption: AES-256-GCM, DEK на запись, обёрнутый мастер-ключом (ТЗ S-1..S-3).

- DEK генерируется на каждую запись; данные шифруются DEK с associated data;
- DEK шифруется (оборачивается) активным KEK, в строке хранится `key_id`;
- associated data = (таблица, владелец, слот/фактор): blob нельзя перенести в чужую строку (S-2);
- ротация KEK — `rewrap()`: DEK переоборачивается новым ключом, шифротекст данных не меняется (S-3).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import pydantic_core
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel, SecretBytes, SecretStr

from identity_hub.errors import SecretConfigurationError
from identity_hub.protocols import SecretsProvider

__all__ = ["DecryptionFailed", "EncryptedPayload", "Envelope", "associated_data", "reveal"]

NONCE_BYTES = 12


class DecryptionFailed(SecretConfigurationError):
    """Неверный ключ, подмена данных или associated data."""

    code = "decryption_failed"


@dataclass(frozen=True, slots=True)
class EncryptedPayload:
    """Колонки EncryptedPayloadMixin: key_id, wrapped_dek, nonce, ciphertext."""

    key_id: str
    wrapped_dek: bytes
    nonce: bytes
    ciphertext: bytes

    def __repr__(self) -> str:
        return f"EncryptedPayload(key_id={self.key_id!r}, {len(self.ciphertext)} bytes)"

    def columns(self, prefix: str = "") -> dict[str, Any]:
        return {
            f"{prefix}key_id": self.key_id,
            f"{prefix}wrapped_dek": self.wrapped_dek,
            f"{prefix}nonce": self.nonce,
            f"{prefix}ciphertext": self.ciphertext,
        }


def associated_data(table: str, owner_id: UUID | str, slot: str = "") -> bytes:
    """AAD = (таблица, identity_id/владелец, slot/factor) — ТЗ S-2."""
    return f"ih|{table}|{owner_id}|{slot}".encode()


def reveal(value: Any) -> Any:
    """Рекурсивно раскрыть SecretStr/SecretBytes (только для шифрования, никогда для логов)."""
    if isinstance(value, SecretStr | SecretBytes):
        return value.get_secret_value()
    if isinstance(value, BaseModel):
        return reveal(dict(value))
    if isinstance(value, Mapping):
        return {k: reveal(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [reveal(v) for v in value]
    return value


class Envelope:
    """Шифрование записей библиотеки ключами из `SecretsProvider`."""

    def __init__(self, provider: SecretsProvider) -> None:
        self._provider = provider

    @property
    def active_key_id(self) -> str:
        return self._provider.active_key_id()

    def _kek(self, key_id: str) -> AESGCM:
        try:
            return AESGCM(self._provider.keys()[key_id])
        except KeyError:
            raise DecryptionFailed(f"мастер-ключ {key_id} недоступен") from None

    @staticmethod
    def _wrap_aad(key_id: str, aad: bytes) -> bytes:
        return b"dek|" + key_id.encode() + b"|" + aad

    # --------------------------------------------------------- байты

    def seal(self, plaintext: bytes, aad: bytes) -> EncryptedPayload:
        key_id = self.active_key_id
        dek = AESGCM.generate_key(bit_length=256)
        nonce = os.urandom(NONCE_BYTES)
        ciphertext = AESGCM(dek).encrypt(nonce, plaintext, aad)
        wrapped = self._wrap(key_id, dek, aad)
        return EncryptedPayload(key_id=key_id, wrapped_dek=wrapped, nonce=nonce, ciphertext=ciphertext)

    def open(self, payload: EncryptedPayload, aad: bytes) -> bytes:
        dek = self._unwrap(payload, aad)
        try:
            return AESGCM(dek).decrypt(payload.nonce, payload.ciphertext, aad)
        except InvalidTag:
            raise DecryptionFailed("данные повреждены или принадлежат другой строке") from None

    def rewrap(self, payload: EncryptedPayload, aad: bytes) -> EncryptedPayload:
        """Переобернуть DEK активным мастер-ключом (ротация, S-3). Данные не расшифровываются."""
        if payload.key_id == self.active_key_id:
            return payload
        dek = self._unwrap(payload, aad)
        key_id = self.active_key_id
        return EncryptedPayload(
            key_id=key_id,
            wrapped_dek=self._wrap(key_id, dek, aad),
            nonce=payload.nonce,
            ciphertext=payload.ciphertext,
        )

    def seal_sibling(
        self, payload: EncryptedPayload, aad: bytes, plaintext: bytes, sibling: str
    ) -> tuple[bytes, bytes]:
        """Зашифровать доп. поле тем же DEK (ответ на challenge рядом с контекстом) → (nonce, ciphertext)."""
        dek = self._unwrap(payload, aad)
        nonce = os.urandom(NONCE_BYTES)
        return nonce, AESGCM(dek).encrypt(nonce, plaintext, aad + b"|" + sibling.encode())

    def open_sibling(
        self, payload: EncryptedPayload, aad: bytes, nonce: bytes, ciphertext: bytes, sibling: str
    ) -> bytes:
        dek = self._unwrap(payload, aad)
        try:
            return AESGCM(dek).decrypt(nonce, ciphertext, aad + b"|" + sibling.encode())
        except InvalidTag:
            raise DecryptionFailed("данные повреждены или принадлежат другой строке") from None

    # --------------------------------------------------------- модели

    def seal_model(self, model: BaseModel | Mapping[str, Any], aad: bytes) -> EncryptedPayload:
        return self.seal(pydantic_core.to_json(reveal(model)), aad)

    def open_model[M: BaseModel](self, schema: type[M], payload: EncryptedPayload, aad: bytes) -> M:
        return schema.model_validate_json(self.open(payload, aad))

    def open_json(self, payload: EncryptedPayload, aad: bytes) -> Any:
        return pydantic_core.from_json(self.open(payload, aad))

    # --------------------------------------------------------- DEK

    def _wrap(self, key_id: str, dek: bytes, aad: bytes) -> bytes:
        nonce = os.urandom(NONCE_BYTES)
        return nonce + self._kek(key_id).encrypt(nonce, dek, self._wrap_aad(key_id, aad))

    def _unwrap(self, payload: EncryptedPayload, aad: bytes) -> bytes:
        nonce, wrapped = payload.wrapped_dek[:NONCE_BYTES], payload.wrapped_dek[NONCE_BYTES:]
        try:
            return self._kek(payload.key_id).decrypt(nonce, wrapped, self._wrap_aad(payload.key_id, aad))
        except InvalidTag:
            raise DecryptionFailed("неверный мастер-ключ или associated data") from None
