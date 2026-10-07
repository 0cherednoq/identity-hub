"""F0.2 (R1-19): CI библиотеки проверяет все заявленные версии Python и собранный пакет."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def _job(name: str, workflow: str = "library-ci.yml") -> str:
    text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
    jobs = re.split(r"^  ([\w-]+):\n", text.split("\njobs:\n", maxsplit=1)[1], flags=re.MULTILINE)
    return dict(zip(jobs[1::2], jobs[2::2], strict=True))[name]


def _matrix(job: str) -> set[str]:
    versions = re.search(r"python-version: \[(.+)\]", job)
    assert versions is not None
    return set(re.findall(r'"([\d.]+)"', versions.group(1)))


def test_fast_jobs_cover_supported_python_bounds() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    declared = {
        classifier.rsplit(" :: ", 1)[1]
        for classifier in project["classifiers"]
        if re.fullmatch(r"Programming Language :: Python :: 3\.\d+", classifier)
    }
    job = _job("fast-tests")
    tested = _matrix(job)
    assert tested == {min(declared), max(declared)}
    assert "IH_TEST_BACKENDS: memory" in job
    assert "IH_STORE_COVERAGE" in job
    assert "not pg and not load" in job


def test_package_job_installs_wheel_and_runs_examples() -> None:
    job = _job("package")
    oldest = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "requires-python"
    ]
    assert f'python-version: "{oldest.removeprefix(">=")}"' in job
    assert "uv build" in job
    assert "twine check" in job
    assert "scripts/check_package.sh" in job
    script = (ROOT / "scripts" / "check_package.sh").read_text(encoding="utf-8")
    for example in ("telegram_service.py", "account_lifecycle.py"):
        assert example in script


def test_pgbouncer_job_runs_smoke_set_through_transaction_pooling() -> None:
    """F4.5 (R2-D04): рецепт PgBouncer проверяется в CI, миграции идут мимо него."""
    job = _job("pgbouncer", "extended-tests.yml")
    assert "pgbouncer" in job and "POOL_MODE: transaction" in job
    assert 'IH_TEST_PGBOUNCER: "1"' in job
    assert "IH_TEST_MIGRATE_DATABASE_URL" in job and ":5432/" in job and ":6432/" in job
    assert "tests/pg/test_pgbouncer.py" in job


def test_postgres_job_enforces_store_method_coverage() -> None:
    """F5.3b (R2-P23): один из прогонов check учитывает вызовы всех методов Store."""
    job = _job("postgres")
    assert "IH_STORE_COVERAGE" in job
    assert "IH_TEST_BACKENDS: postgres" in job
    conftest = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert "tests.store_coverage" in conftest and "IH_STORE_COVERAGE" in conftest


def test_release_uses_verified_artifact_and_trusted_publishing() -> None:
    workflow = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    assert "types: [published]" in workflow
    assert "Verify tag matches package version" in workflow
    assert "twine check" in workflow and "scripts/check_package.sh" in workflow
    assert "actions/attest@" in workflow and "# v4" in workflow
    assert "environment:\n      name: pypi" in workflow
    assert "id-token: write" in _job("pypi", "release.yml")
    assert "pypa/gh-action-pypi-publish@" in workflow and "# release/v1" in workflow
