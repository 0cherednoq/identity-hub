"""Шифрование секретов Identity Hub (ТЗ §9)."""

from identity_hub.crypto.envelope import DecryptionFailed, EncryptedPayload, Envelope, associated_data, reveal
from identity_hub.crypto.providers import (
    EnvSecretsProvider,
    FileSecretsProvider,
    StaticSecretsProvider,
    generate_key,
    parse_keys,
)
from identity_hub.crypto.secret_value import SecretValue

__all__ = [
    "DecryptionFailed",
    "EncryptedPayload",
    "EnvSecretsProvider",
    "Envelope",
    "FileSecretsProvider",
    "SecretValue",
    "StaticSecretsProvider",
    "associated_data",
    "generate_key",
    "parse_keys",
    "reveal",
]
