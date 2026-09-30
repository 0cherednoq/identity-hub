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
    OperationRule,
    Override,
    Pacing,
    Quota,
    Service,
    Severity,
    Standing,
)

ROOT = Path(__file__).parents[1]


def test_release_candidate_version_is_consistent() -> None:
    assert identity_hub.__version__ == "1.0.0a1"
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

    assert service.rule("read_channel").needs_auth
    assert pacing.work.limit == 40
    assert override.state is Standing.GOOD
    assert str(Severity.WARNING) == "warning"
    assert IdentityHub is identity_hub.IdentityHub
    assert EnvSecretsProvider is identity_hub.EnvSecretsProvider


def test_unsupported_quota_reset_is_not_public() -> None:
    assert "reset_quotas" not in identity_hub.__all__
