"""Шифрование секретов Identity Hub (ТЗ §9)."""

from identity_hub.crypto.envelope import DecryptionFailed, EncryptedPayload, Envelope, associated_data, reveal
from identity_hub.crypto.providers import (
    EnvSecretsProvider,
    FileSecretsProvider,
    StaticSecretsProvider,
    generate_key,
    parse_keys,
)

__all__ = [
    "DecryptionFailed",
    "EncryptedPayload",
    "EnvSecretsProvider",
    "Envelope",
    "FileSecretsProvider",
    "StaticSecretsProvider",
    "associated_data",
    "generate_key",
    "parse_keys",
    "reveal",
]
