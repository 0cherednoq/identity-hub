"""F0.2 (R1-19): CI библиотеки проверяет все заявленные версии Python и собранный пакет."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "library-ci.yml"


def _job(name: str) -> str:
    text = WORKFLOW.read_text(encoding="utf-8")
    jobs = re.split(r"^  (\w+):\n", text.split("\njobs:\n", maxsplit=1)[1], flags=re.MULTILINE)
    return dict(zip(jobs[1::2], jobs[2::2], strict=True))[name]


def _matrix(job: str) -> set[str]:
    versions = re.search(r"python-version: \[(.+)\]", job)
    assert versions is not None
    return set(re.findall(r'"([\d.]+)"', versions.group(1)))


def test_check_job_covers_every_declared_python() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    declared = {
        classifier.rsplit(" :: ", 1)[1]
        for classifier in project["classifiers"]
        if re.fullmatch(r"Programming Language :: Python :: 3\.\d+", classifier)
    }
    job = _job("check")
    assert _matrix(job) == declared
    assert "bash scripts/check.sh" in job
    assert "postgres:16" in job


def test_package_job_installs_wheel_and_runs_examples() -> None:
    job = _job("package")
    oldest = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "requires-python"
    ]
    assert oldest.removeprefix(">=") in _matrix(job)
    assert "uv build" in job
    assert "scripts/check_package.sh" in job
    script = (ROOT / "scripts" / "check_package.sh").read_text(encoding="utf-8")
    for example in ("telegram_service.py", "account_lifecycle.py"):
        assert example in script


def test_pgbouncer_job_runs_smoke_set_through_transaction_pooling() -> None:
    """F4.5 (R2-D04): рецепт PgBouncer проверяется в CI, миграции идут мимо него."""
    job = _job("pgbouncer")
    assert "pgbouncer" in job and "POOL_MODE: transaction" in job
    assert 'IH_TEST_PGBOUNCER: "1"' in job
    assert "IH_TEST_MIGRATE_DATABASE_URL" in job and ":5432/" in job and ":6432/" in job
    assert "tests/pg/test_pgbouncer.py" in job
