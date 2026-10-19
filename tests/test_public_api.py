"""P10.5: стабильная корневая поверхность и версия release candidate."""

from __future__ import annotations

import tomllib
from datetime import timedelta
from importlib.metadata import version
from pathlib import Path

from pydantic import BaseModel

import identity_hub
from identity_hub import (
    EnvSecretsProvider,
    IdentityHub,
    Observation,
    OperationRule,
    Override,
    Pacing,
    Quota,
    Service,
    Severity,
    Standing,
    UpdateManyResult,
)

ROOT = Path(__file__).parents[1]


def test_release_candidate_version_is_consistent() -> None:
    assert identity_hub.__version__ == "1.0.0a5"
    assert version("identity-hub") == identity_hub.__version__
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert metadata["project"]["version"] == identity_hub.__version__


def test_all_exports_exist_are_unique_and_public() -> None:
    assert len(identity_hub.__all__) == len(set(identity_hub.__all__))
    assert all(hasattr(identity_hub, name) for name in identity_hub.__all__)
    assert all(not name.startswith("_") or name == "__version__" for name in identity_hub.__all__)


def test_section_11_types_are_importable_from_package_root() -> None:
    class Session(BaseModel):
        token: str

    service = Service(
        name="telegram",
        title="Telegram",
        session=Session,
        operations={"read_channel": OperationRule()},
    )
    pacing = Pacing(work=Quota(40, per=timedelta(hours=1)), pause=(15, 25))
    override = Override(
        "standing",
        "",
        Standing.GOOD,
        reason="ложная блокировка",
        actor="test",
    )
    observation = Observation("standing", "", Standing.LIMITED)
    bulk = UpdateManyResult(changed=2, skipped=1)

    assert service.rule("read_channel").needs_auth
    assert pacing.work.limit == 40
    assert override.state is Standing.GOOD
    assert observation.state is Standing.LIMITED
    assert (bulk.changed, bulk.skipped) == (2, 1)
    assert str(Severity.WARNING) == "warning"
    assert IdentityHub is identity_hub.IdentityHub
    assert EnvSecretsProvider is identity_hub.EnvSecretsProvider


def test_unsupported_quota_reset_is_not_public() -> None:
    assert "reset_quotas" not in identity_hub.__all__


def test_standard_format_exports_are_allowlisted_and_documented() -> None:
    names = {
        "ApiKey",
        "BrowserFingerprint",
        "BrowserSession",
        "ClientCertificate",
        "Cookie",
        "Cookies",
        "DumpNamespace",
        "FormatLossWarning",
        "Geolocation",
        "HeaderSession",
        "LoadNamespace",
        "OAuth1Client",
        "OAuth1Session",
        "OAuthClient",
        "OriginStorage",
        "OtpSecret",
        "PartitionKey",
        "PasswordCredentials",
        "PrivateKeyCredentials",
        "ProxyUrl",
        "RecoveryCodes",
        "SameSite",
        "Screen",
        "SigningKey",
        "TokenSession",
        "UserAgentData",
        "Viewport",
        "WebGl",
    }
    assert names <= set(identity_hub.__all__)
    guide = (ROOT / "docs" / "guide" / "api.md").read_text(encoding="utf-8")
    assert [name for name in sorted(names) if f"`{name}`" not in guide] == []


def test_import_merge_and_credential_validity_contract_is_public_and_documented() -> None:
    from identity_hub import CredentialCandidate, ImportReport, LoginContext, OnExisting

    names = {"CredentialCandidate", "ImportReport", "LoginContext", "LoginFailure", "OnExisting"}
    assert names <= set(identity_hub.__all__)
    assert CredentialCandidate("api_key", "trading", object()).key == ("api_key", "trading")
    assert OnExisting.MERGE == "merge"
    assert "merge_details" in ImportReport.__dataclass_fields__
    assert {"created_revisions", "updated_revisions"} <= ImportReport.__dataclass_fields__.keys()
    assert {"active_credential", "verify_credential", "reject_credential"} <= set(LoginContext.__dict__)
    guide = (ROOT / "docs" / "guide" / "api.md").read_text(encoding="utf-8")
    assert [name for name in sorted(names) if f"`{name}`" not in guide] == []


def test_consumer_cursor_expired_is_public_and_documented() -> None:
    from identity_hub import ConsumerCursorExpired

    error = ConsumerCursorExpired("projection", "1:1", "2:2")
    assert error.code == "consumer_cursor_expired"
    assert error.context == {"consumer": "projection", "cursor": "1:1", "oldest": "2:2"}
    guide = (ROOT / "docs" / "guide" / "10-events.md").read_text(encoding="utf-8")
    assert "`ConsumerCursorExpired`" in guide


def test_link_filter_contract_is_public_and_documented() -> None:
    from identity_hub import IdentityFilter

    fields = IdentityFilter.__dataclass_fields__
    assert {"without_link", "not_target_of", "is_target_of"} <= set(fields)
    guide = (ROOT / "docs" / "guide" / "api.md").read_text(encoding="utf-8")
    assert all(f"`{name}" in guide for name in ("without_link", "not_target_of", "is_target_of"))


def test_eligible_capacity_contract_is_public_and_documented() -> None:
    from identity_hub import EligibleCapacity, Pool

    assert "EligibleCapacity" in identity_hub.__all__
    assert set(EligibleCapacity.__dataclass_fields__) == {
        "eligible",
        "free",
        "leased",
        "reserved",
        "allocatable",
    }
    assert hasattr(Pool, "eligible_count")

    api = (ROOT / "docs" / "guide" / "api.md").read_text(encoding="utf-8")
    leases = (ROOT / "docs" / "guide" / "07-leases.md").read_text(encoding="utf-8")
    for name in ("EligibleCapacity", "eligible_count", "allocatable", "require_quota", "limit"):
        assert f"`{name}" in api
        assert f"`{name}" in leases
    assert "SQLAlchemy `Select`" in api
    assert "worker_accounts.c.identity_id" in leases


def test_all_lease_unavailable_reasons_are_documented_with_defer_strategy() -> None:
    from identity_hub import LeaseUnavailableReason

    leases = (ROOT / "docs" / "guide" / "07-leases.md").read_text(encoding="utf-8")
    handoff = (ROOT / "docs" / "resource-management-tallyho-handoff.md").read_text(encoding="utf-8")
    for reason in LeaseUnavailableReason:
        assert f"| `{reason.value}` |" in leases
        assert f"| `{reason.value}` |" in handoff
    assert "LeaseUnavailable.retry_after" in leases
    assert "QuotaExhausted.retry_at" in leases
    assert "Defer-стратегия" in leases
    assert "IdentityCreate.service" in handoff
    assert "service=SERVICE" in handoff


def test_optional_eazy_integration_is_importable_and_documented() -> None:
    from identity_hub.integrations import eazy

    assert set(eazy.__all__) == {"EazySession", "HubSessionStore"}
    guide = (ROOT / "docs" / "guide" / "api.md").read_text(encoding="utf-8")
    assert all(f"`{name}" in guide for name in eazy.__all__)


HUB_PUBLIC = {
    # жизненный цикл и реестр сервисов
    "start",
    "aclose",
    "register_service",
    "service",
    "services",
    # пространства и сводные read-модели
    "space",
    "spaces",
    "get_space",
    "list_spaces",
    "spaces_matrix",
    "service_catalog",
    "background_snapshot",
    # события, фоновые процессы, диагностика
    "read",
    "events",
    "dispatcher",
    "maintenance",
    "workers",
    "doctor",
    "equivalence_tag",
    "metrics",
    "lease_log_sink",
    "flush_lease_losses",
    "flush_metrics",
    # настройки, заданные конструктором
    "clock",
    "retention",
    "timezone",
    "admin_base_url",
    "db_schema",
    "proxy_health",
    "strict_events",
    "redact_refs_in_events",
    "webhook_dev_hosts",
    "webhook_policy",
    "sessionmaker",
}


def test_hub_public_surface_is_fixed_and_documented() -> None:
    """R1-29: контракт хаба — явный список; хранилище, шифрование и служебные объекты приватны."""
    from identity_hub import StaticSecretsProvider
    from identity_hub.testing import MemoryStore

    hub = IdentityHub(store=MemoryStore(), secrets=StaticSecretsProvider({"k": b"\x01" * 32}, "k"))
    public = {name for name in dir(hub) if not name.startswith("_")}

    assert public == HUB_PUBLIC
    for internal in ("store", "envelope", "import_runner", "metric_rollups", "rollups", "state_snapshots"):
        assert not hasattr(hub, internal)  # R2-S10: расшифровка в обход аудита недоступна
    assert hub.sessionmaker is None and hub.webhook_policy.max_failures > 0

    guide = (ROOT / "docs" / "guide" / "api.md").read_text(encoding="utf-8")
    assert [name for name in sorted(HUB_PUBLIC) if f"`hub.{name}" not in guide] == []
