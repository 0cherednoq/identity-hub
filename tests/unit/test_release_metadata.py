from __future__ import annotations

from pathlib import Path

import pytest
from scripts.check_release_metadata import changelog_schema, validate_metadata

ROOT = Path(__file__).parents[2]

CHANGELOG = """\
## [Unreleased]

## [1.2.3] - 2026-10-16

Ревизия схемы — `0042`: примените миграции.

### Added

- Изменение.
"""


def test_release_metadata_accepts_matching_values() -> None:
    validate_metadata(
        package_version="1.2.3",
        source_version="1.2.3",
        schema_head="0042",
        changelog=CHANGELOG,
        release_tag="v1.2.3",
    )
    assert changelog_schema(CHANGELOG, "1.2.3") == "0042"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"source_version": "1.2.4"}, "src version"),
        ({"schema_head": "0043"}, "schema 0042"),
        ({"release_tag": "v1.2.4"}, "release tag"),
        ({"changelog": "## [Unreleased]\n"}, "нет секции"),
    ],
)
def test_release_metadata_rejects_drift(overrides: dict[str, str], message: str) -> None:
    values = {
        "package_version": "1.2.3",
        "source_version": "1.2.3",
        "schema_head": "0042",
        "changelog": CHANGELOG,
        "release_tag": "v1.2.3",
    }
    values.update(overrides)
    with pytest.raises(ValueError, match=message):
        validate_metadata(**values)


def test_release_workflow_checks_metadata_before_build() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    check = 'python scripts/check_release_metadata.py --tag "$RELEASE_TAG"'
    assert check in workflow
    assert workflow.index(check) < workflow.index("uv build --out-dir dist")
