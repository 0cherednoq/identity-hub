"""Envelope encryption: AES-256-GCM, DEK на запись, обёрнутый мастер-ключом (ТЗ S-1..S-3).

- DEK генерируется на каждую запись; данные шифруются DEK с associated data;
- DEK шифруется (оборачивается) активным KEK, в строке хранится `key_id`;
- associated data = `identity-hub:v1:<таблица>:<владелец>:<слот>:<версия схемы>`: blob нельзя
  перенести в чужую строку (S-2); записи `1.0.0a1` с AAD `ih|<таблица>|<владелец>|<слот>`
  читаются, пока не будут перезаписаны (IMP-16);
- ротация KEK — `rewrap()`: DEK переоборачивается новым ключом (S-3); запись со старым форматом
  AAD при этом перешифровывается в новый;
- лимиты: plaintext секрета до 512 KiB, envelope до 1 MiB (IMP-16).
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

from identity_hub.crypto.secret_value import SecretValue
from identity_hub.errors import SecretConfigurationError, ValidationFailed
from identity_hub.protocols import SecretsProvider

__all__ = [
    "AAD_VERSION",
    "MAX_ENVELOPE_BYTES",
    "MAX_SECRET_BYTES",
    "AssociatedData",
    "DecryptionFailed",
    "EncryptedPayload",
    "Envelope",
    "associated_data",
    "credential_associated_data",
    "reveal",
]

NONCE_BYTES = 12
AAD_VERSION = "v1"
MAX_SECRET_BYTES = 512 * 1024
MAX_ENVELOPE_BYTES = 1024 * 1024


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


class AssociatedData(bytes):
    """AAD текущего формата; `legacy` — формат `1.0.0a1` для чтения ещё не перезаписанных строк."""

    legacy: bytes

    def __new__(cls, value: bytes, legacy: bytes) -> AssociatedData:
        instance = super().__new__(cls, value)
        instance.legacy = legacy
        return instance


def associated_data(
    table: str, owner_id: UUID | str, slot: str = "", *, schema_version: int = 1
) -> AssociatedData:
    """AAD = (таблица, владелец, slot/factor, версия схемы полезной нагрузки) — ТЗ S-2, IMP-16."""
    current = f"identity-hub:{AAD_VERSION}:{table}:{owner_id}:{slot}:{schema_version}".encode()
    return AssociatedData(current, f"ih|{table}|{owner_id}|{slot}".encode())


DEFAULT_CREDENTIAL = ("default", "default")


def credential_associated_data(identity_id: UUID | str, kind: str, name: str) -> AssociatedData:
    """AAD записи credentials (D-214): у `default`/`default` — прежний, без slot, иначе `kind/name`."""
    slot = "" if (kind, name) == DEFAULT_CREDENTIAL else f"{kind}/{name}"
    return associated_data("ih_credentials", identity_id, slot)


def _candidates(aad: bytes) -> list[bytes]:
    """Какие AAD пробовать при чтении: текущий формат, затем legacy."""
    return [bytes(aad), aad.legacy] if isinstance(aad, AssociatedData) else [aad]


def reveal(value: Any) -> Any:
    """Рекурсивно раскрыть SecretStr/SecretBytes (только для шифрования, никогда для логов)."""
    if isinstance(value, SecretStr | SecretBytes):
        return value.get_secret_value()
    if isinstance(value, SecretValue):
        revealed = value.reveal()
        return revealed.decode() if isinstance(revealed, bytes) else revealed
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
        if len(plaintext) > MAX_SECRET_BYTES:
            raise ValidationFailed(f"секрет больше {MAX_SECRET_BYTES // 1024} KiB")
        key_id = self.active_key_id
        dek = AESGCM.generate_key(bit_length=256)
        nonce = os.urandom(NONCE_BYTES)
        ciphertext = AESGCM(dek).encrypt(nonce, plaintext, aad)
        wrapped = self._wrap(key_id, dek, aad)
        if (
            len(wrapped) + len(nonce) + len(ciphertext) > MAX_ENVELOPE_BYTES
        ):  # pragma: no cover - при лимите секрета недостижимо
            raise ValidationFailed(f"envelope больше {MAX_ENVELOPE_BYTES // 1024} KiB")
        return EncryptedPayload(key_id=key_id, wrapped_dek=wrapped, nonce=nonce, ciphertext=ciphertext)

    def open(self, payload: EncryptedPayload, aad: bytes) -> bytes:
        plaintext, _used = self._open_any(payload, aad)
        return plaintext

    def _open_any(self, payload: EncryptedPayload, aad: bytes) -> tuple[bytes, bytes]:
        """Расшифровать текущим AAD или legacy → (plaintext, сработавший AAD)."""
        error: DecryptionFailed | None = None
        for candidate in _candidates(aad):
            try:
                dek = self._unwrap(payload, candidate)
                return AESGCM(dek).decrypt(payload.nonce, payload.ciphertext, candidate), candidate
            except DecryptionFailed as exc:
                error = exc
            except InvalidTag:
                error = DecryptionFailed("данные повреждены или принадлежат другой строке")
        assert error is not None
        raise error from None

    def _dek_any(self, payload: EncryptedPayload, aad: bytes) -> tuple[bytes, bytes]:
        error: DecryptionFailed | None = None
        for candidate in _candidates(aad):
            try:
                return self._unwrap(payload, candidate), candidate
            except DecryptionFailed as exc:
                error = exc
        assert error is not None
        raise error

    def rewrap(self, payload: EncryptedPayload, aad: bytes, *, upgrade: bool = True) -> EncryptedPayload:
        """Переобернуть DEK активным мастер-ключом (ротация, S-3).

        Шифротекст не меняется, если строка уже в текущем формате AAD; запись `1.0.0a1`
        перешифровывается под текущий AAD (новые DEK и nonce). `upgrade=False` — только
        переобернуть DEK: для записей, рядом с которыми тем же DEK зашифровано ещё поле
        (ответ на challenge, `seal_sibling`).
        """
        dek, used = self._dek_any(payload, aad)
        if used != bytes(aad) and upgrade:
            plaintext, _ = self._open_any(payload, aad)
            return self.seal(plaintext, aad)
        if payload.key_id == self.active_key_id:
            return payload
        key_id = self.active_key_id
        return EncryptedPayload(
            key_id=key_id,
            wrapped_dek=self._wrap(key_id, dek, used),
            nonce=payload.nonce,
            ciphertext=payload.ciphertext,
        )

    def seal_sibling(
        self, payload: EncryptedPayload, aad: bytes, plaintext: bytes, sibling: str
    ) -> tuple[bytes, bytes]:
        """Зашифровать доп. поле тем же DEK (ответ на challenge рядом с контекстом) → (nonce, ciphertext)."""
        dek, used = self._dek_any(payload, aad)
        nonce = os.urandom(NONCE_BYTES)
        return nonce, AESGCM(dek).encrypt(nonce, plaintext, used + b"|" + sibling.encode())

    def open_sibling(
        self, payload: EncryptedPayload, aad: bytes, nonce: bytes, ciphertext: bytes, sibling: str
    ) -> bytes:
        dek, used = self._dek_any(payload, aad)
        try:
            return AESGCM(dek).decrypt(nonce, ciphertext, used + b"|" + sibling.encode())
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
