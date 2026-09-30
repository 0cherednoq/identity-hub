"""IMP-19 (I0.2b): архитектурные границы пакета проверяются import-linter."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]


def _contracts() -> list[dict[str, object]]:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    contracts: list[dict[str, object]] = config["tool"]["importlinter"]["contracts"]
    return contracts


def test_import_contracts() -> None:
    lint = Path(sys.executable).with_name("lint-imports.exe" if sys.platform == "win32" else "lint-imports")
    result = subprocess.run(
        [str(lint)], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )
    assert result.returncode == 0, result.stdout[-2000:]
    assert f"Contracts: {len(_contracts())} kept, 0 broken." in result.stdout


def test_domain_contract_forbids_infrastructure() -> None:
    domain = next(c for c in _contracts() if c["source_modules"] == ["identity_hub.domain"])
    forbidden = set(domain["forbidden_modules"])  # type: ignore[arg-type]
    assert {"sqlalchemy", "alembic", "httpx", "aiogram", "identity_hub.postgres"} <= forbidden


def test_integrations_and_store_contract_do_not_import_postgres() -> None:
    contract = next(c for c in _contracts() if "identity_hub.store" in c["source_modules"])  # type: ignore[operator]
    assert {"identity_hub.api", "identity_hub.read", "identity_hub.dispatch", "identity_hub.testing"} <= set(
        contract["source_modules"]  # type: ignore[arg-type]
    )
    assert contract["forbidden_modules"] == ["identity_hub.postgres"]
