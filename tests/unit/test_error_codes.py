"""IMP-13 (I0.2a): стабильные коды, безопасный context, отображение в HTTP и CLI."""

from __future__ import annotations

import argparse
import inspect
import json
import pickle
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

import identity_hub
from identity_hub import cli, errors
from identity_hub.crypto import DecryptionFailed
from identity_hub.errors import (
    AlreadyExists,
    Blocker,
    ConcurrentModification,
    IdentityHubError,
    LeaseExpired,
    LeaseLost,
    LeaseOwnershipLost,
    LeaseRevoked,
    NotFound,
    OperationNotAllowed,
    QuotaExhausted,
    RevisionConflict,
    SchemaIncompatible,
    SecretConfigurationError,
    ValidationFailed,
    exit_code,
    http_status,
)

# Коды — публичный контракт: менять только новой записью D-NNN и в migration guide.
EXPECTED_CODES = {
    "IdentityHubError": "internal_error",
    "NotFound": "not_found",
    "AlreadyExists": "already_exists",
    "SpaceMismatch": "space_mismatch",
    "ValidationFailed": "validation_error",
    "TransitionNotAllowed": "invalid_transition",
    "ConcurrentModification": "concurrent_modification",
    "RevisionConflict": "revision_conflict",
    "IdempotencyConflict": "idempotency_conflict",
    "OperationNotAllowed": "operation_not_allowed",
    "QuotaExhausted": "quota_exceeded",
    "LeaseUnavailable": "no_resource_available",
    "LeaseLost": "lease_lost",
    "LeaseOwnershipLost": "lease_ownership_lost",
    "LeaseRevoked": "lease_revoked",
    "LeaseExpired": "lease_expired",
    "StaleFencing": "stale_fencing",
    "ChallengeAlreadyOpen": "challenge_already_open",
    "VerificationRequirementsNotMet": "verification_requirements_not_met",
    "AttemptsExhausted": "attempts_exhausted",
    "DatabaseBusy": "database_busy",
    "StorageError": "storage_error",
    "SchemaIncompatible": "schema_incompatible",
    "SecretConfigurationError": "secret_configuration_error",
    "DecryptionFailed": "decryption_failed",
}


def _public_error_classes() -> dict[str, type[IdentityHubError]]:
    found = {
        name: obj
        for name, obj in vars(identity_hub).items()
        if inspect.isclass(obj) and issubclass(obj, IdentityHubError)
    }
    found["DecryptionFailed"] = DecryptionFailed
    return found


def test_every_error_has_stable_code() -> None:
    classes = _public_error_classes()
    assert {name: cls.code for name, cls in classes.items()} == EXPECTED_CODES
    codes = [cls.code for cls in classes.values()]
    assert len(codes) == len(set(codes)), "два класса с одним кодом"


def test_every_code_has_http_status() -> None:
    for name, cls in _public_error_classes().items():
        assert cls.code in errors._HTTP_STATUS, name


@pytest.mark.parametrize(
    ("error", "status", "exit"),
    [
        (ValidationFailed("x"), 422, 2),
        (NotFound("identity", uuid4()), 404, 2),
        (identity_hub.LeaseUnavailable("нет"), 503, 1),  # F6.2a (R1-27): временная недоступность
        (RevisionConflict("identity", 1, 2, 3), 409, 1),
        (LeaseRevoked(uuid4(), "admin"), 409, 1),
        (QuotaExhausted("send", 60), 429, 1),
        (identity_hub.DatabaseBusy("lock timeout"), 503, 4),
        (identity_hub.DatabaseBusy(sqlstate="57014"), 503, 4),
        (identity_hub.StorageError("23505"), 500, 1),
        (SchemaIncompatible("0007", "0005"), 503, 3),
        (SecretConfigurationError("нет ключа"), 500, 5),
        (DecryptionFailed("bad tag"), 500, 5),
        (IdentityHubError("?"), 500, 1),
    ],
    ids=lambda v: getattr(v, "code", str(v)),
)
def test_http_and_exit_mapping(error: IdentityHubError, status: int, exit: int) -> None:
    assert http_status(error) == status
    assert exit_code(error) == exit


def test_subclasses_keep_catchable_parents() -> None:
    assert issubclass(RevisionConflict, ConcurrentModification)
    assert all(issubclass(cls, LeaseLost) for cls in (LeaseOwnershipLost, LeaseRevoked, LeaseExpired))
    assert issubclass(DecryptionFailed, SecretConfigurationError)


def test_context_is_json_safe_and_limited_to_declared_fields() -> None:
    identity_id = uuid4()
    when = datetime(2026, 9, 30, tzinfo=UTC)
    blocked = OperationNotAllowed("send", [Blocker("standing", "banned", when), Blocker("quota", "limit")])
    cases: list[tuple[IdentityHubError, dict[str, Any]]] = [
        (NotFound("identity", identity_id), {"entity": "identity", "key": str(identity_id)}),
        (
            AlreadyExists("identity", "fake_tg:+1", identity_id),
            {"entity": "identity", "key": "fake_tg:+1", "existing_id": str(identity_id)},
        ),
        (
            QuotaExhausted("send", 60, when),
            {"kind": "send", "window_seconds": 60, "retry_at": when.isoformat()},
        ),
        (blocked, {"kind": "send", "blocker_codes": ["standing", "quota"]}),
        (
            ValidationFailed("bad", ["profile.birthday: Input should be a valid date"]),
            {"errors": ["profile.birthday: Input should be a valid date"]},
        ),
        (SchemaIncompatible("0007", None), {"expected": "0007", "actual": None}),
    ]
    for error, expected in cases:
        assert error.context == expected
        json.dumps(error.context)


def test_context_never_exposes_idempotency_key_or_unknown_objects() -> None:
    error = identity_hub.IdempotencyConflict("lease.acquire")
    error.key = "secret-idempotency-key"  # type: ignore[attr-defined]
    assert error.context == {"operation": "lease.acquire"}
    assert "secret-idempotency-key" not in str(error)
    assert errors._safe(object()) == "object"


def test_new_errors_survive_pickle() -> None:
    error = RevisionConflict("identity", "k", 1, 2)
    restored = pickle.loads(pickle.dumps(error))
    assert type(restored) is RevisionConflict
    assert restored.context == error.context and restored.code == "revision_conflict"


def test_cli_returns_exit_code_and_prints_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def failing(_args: argparse.Namespace) -> int:
        raise SchemaIncompatible("0007", "0005")

    monkeypatch.setattr(cli, "async_main", failing)
    assert cli.main(["doctor"]) == 3
    assert "error[schema_incompatible]" in capsys.readouterr().err


def test_describe_error_never_uses_foreign_exception_text() -> None:
    assert errors.describe_error(RuntimeError("token=SECRET")) == "RuntimeError"
    assert errors.describe_error(identity_hub.StorageError("23505")) == "StorageError[storage_error]"
