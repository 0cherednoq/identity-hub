"""F6.5 (R1-31, R1-32): публичный контракт, значения по умолчанию, язык и версия совпадают с кодом."""

from __future__ import annotations

import tomllib
from datetime import timedelta
from pathlib import Path

import pytest

import identity_hub
from identity_hub import Retention, Service
from identity_hub.api.identities import CREATE_MANY_LIMIT
from identity_hub.crypto.envelope import MAX_ENVELOPE_BYTES, MAX_SECRET_BYTES
from identity_hub.dispatch.webhooks import WebhookPolicy
from identity_hub.domain.data import MAX_JSON_BYTES
from identity_hub.logging import LeaseLogPolicy
from identity_hub.maintenance import MaintenanceSchedule
from identity_hub.pool import LeaseTiming
from identity_hub.proxies import ProxyHealthPolicy
from identity_hub.read.search import MAX_PAGE

pytestmark = pytest.mark.unit

ROOT = Path(__file__).parents[2]
API = (ROOT / "docs" / "guide" / "api.md").read_text(encoding="utf-8")


def _duration(value: timedelta | float) -> str:
    seconds = int(value.total_seconds() if isinstance(value, timedelta) else value)
    for unit, size in (("дн", 86400), ("ч", 3600), ("мин", 60)):
        if seconds >= size and seconds % size == 0:
            return f"{seconds // size} {unit}"
    return f"{seconds} с"


def _size(value: int) -> str:
    for unit, size in (("МиБ", 1024 * 1024), ("КиБ", 1024)):
        if value % size == 0:
            return f"{value // size} {unit}"
    return str(value)


def _service_default(name: str) -> timedelta:
    value: timedelta = Service.__dataclass_fields__[name].default  # type: ignore[assignment]
    return value


DEFAULTS: dict[str, str] = {
    "LeaseTiming.ttl": _duration(LeaseTiming().ttl),
    "LeaseTiming.heartbeat": _duration(LeaseTiming().heartbeat),
    "LeaseTiming.safety_margin": _duration(LeaseTiming().safety_margin),
    "LeaseTiming.stats_flush": _duration(LeaseTiming().stats_flush),
    "Service.login_timeout": _duration(_service_default("login_timeout")),
    "Service.probe_timeout": _duration(_service_default("probe_timeout")),
    "LoginChallenge.expires_in": _duration(timedelta(hours=1)),
    "ProxyHealthPolicy.error_threshold": str(ProxyHealthPolicy().error_threshold),
    "ProxyHealthPolicy.check_timeout": _duration(ProxyHealthPolicy().check_timeout),
    "ProxyHealthPolicy.max_backoff": _duration(ProxyHealthPolicy().max_backoff),
    "WebhookPolicy.timeout": _duration(WebhookPolicy().timeout),
    "WebhookPolicy.max_failures": str(WebhookPolicy().max_failures),
    "LeaseLogPolicy.max_per_lease": str(LeaseLogPolicy().max_per_lease),
    "MaintenanceSchedule.normalize_seconds": _duration(MaintenanceSchedule().normalize_seconds),
    "Retention.events": _duration(Retention().events),
    "Retention.lease_history": _duration(Retention().lease_history),
    "Retention.operation_keys": _duration(Retention().operation_keys),
    "identities.create_many": str(CREATE_MANY_LIMIT),
    "imports.stage": "10000",
    "limit": str(MAX_PAGE),
    "data.save": _size(MAX_JSON_BYTES),
    "MAX_SECRET_BYTES": _size(MAX_SECRET_BYTES),
    "MAX_ENVELOPE_BYTES": _size(MAX_ENVELOPE_BYTES),
}


def _section(text: str, title: str) -> str:
    start = text.index(title)
    end = text.find("\n## ", start + len(title))
    return text[start : end if end != -1 else len(text)]


@pytest.mark.parametrize(("name", "value"), DEFAULTS.items(), ids=list(DEFAULTS))
def test_defaults_table_matches_code(name: str, value: str) -> None:
    table = _section(API, "## Значения по умолчанию")
    row = next((line for line in table.splitlines() if line.startswith(f"| `{name}`")), None)
    assert row is not None, f"нет строки {name}"
    assert f"| {value} |" in row, f"{name}: в коде {value}, в документации {row}"


def test_public_contract_section_names_every_part() -> None:
    section = _section(API, "## Что входит в публичный контракт")
    for part in (
        "__all__",
        "Неймспейсы пространства",
        "событ",
        "IdentityHubError.code",
        "hub.read",
        "READ_SCHEMA_VERSION",
        "шифртекст",
        "identity-hub",
        "Store",
    ):
        assert part in section, part


def test_version_and_language_are_stated() -> None:
    """D-152: версия в линии 1.0.0aN, документация русская, в README — раздел на английском."""
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    assert version.startswith("1.0.0a")
    assert identity_hub.__version__ == version
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    english = _section(readme, "## In English")
    for part in ("Status", "uv add", "alpha", "Russian"):
        assert part in english, part


def test_schema_upgrade_protocol_is_documented() -> None:
    integrations = (ROOT / "docs" / "integrations.md").read_text(encoding="utf-8")
    protocol = _section(integrations, "### Обновление без простоя")
    assert "SCHEMA_COMPATIBLE" in protocol and "identity-hub migrate" in protocol
    assert "integrations.md#обновление-без-простоя" in API


def test_long_transaction_delay_is_documented() -> None:
    """R1-18: долгая транзакция в базе задерживает доставку всех событий — это сказано в руководстве."""
    events = (ROOT / "docs" / "guide" / "10-events.md").read_text(encoding="utf-8")
    section = events[events.index("**Порядок и долгие транзакции.**") :][:800]
    assert "горизонт" in section and "idle_in_transaction_session_timeout" in section
