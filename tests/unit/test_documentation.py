"""Проверки локальных ссылок в пользовательской документации."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from identity_hub.api._migration import LEGACY

ROOT = Path(__file__).parents[2]
GUIDE = tuple(sorted((ROOT / "docs" / "guide").glob("*.md")))
DOCUMENTS = (
    ROOT / "README.md",
    ROOT / "CHANGELOG.md",
    ROOT / "docs" / "cli.md",
    ROOT / "docs" / "integrations.md",
    ROOT / "docs" / "perf.md",
    ROOT / "docs" / "traceability.md",
    ROOT / "docs" / "api-migration.md",
    *GUIDE,
)
# Документы для пользователя на новом API; старые имена допустимы только в migration guide и CHANGELOG.
CURRENT_API_DOCUMENTS = (
    ROOT / "README.md",
    ROOT / "docs" / "cli.md",
    ROOT / "docs" / "integrations.md",
    *GUIDE,
)
MARKDOWN_LINK = re.compile(r"\[[^]]+]\(([^)]+)\)")


@pytest.mark.unit
@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda path: path.name)
def test_local_documentation_links_exist(document: Path) -> None:
    missing: list[str] = []
    for target in MARKDOWN_LINK.findall(document.read_text(encoding="utf-8")):
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        path = target.split("#", 1)[0]
        if path and not (document.parent / path).resolve().exists():
            missing.append(target)

    assert not missing, f"{document.relative_to(ROOT)}: отсутствуют {missing}"


LEGACY_NAMES = "|".join(sorted(LEGACY, key=len, reverse=True))
LEGACY_CALL = re.compile(rf"\b(?:space|handle|identity_hub_space)(?:\.using\([^)]*\))?\.({LEGACY_NAMES})\(")
LEGACY_FORMS = re.compile(r"ProxyPolicy\(\s*pool=|\.check_capacity\(")


@pytest.mark.unit
@pytest.mark.parametrize("document", CURRENT_API_DOCUMENTS, ids=lambda path: path.name)
def test_documentation_uses_current_api(document: Path) -> None:
    """IMP-23 (I8.9): примеры и текст не показывают устаревшие методы пространства."""
    text = document.read_text(encoding="utf-8")
    found = sorted({match.group(1) for match in LEGACY_CALL.finditer(text)} | set(LEGACY_FORMS.findall(text)))

    assert not found, f"{document.relative_to(ROOT)}: устаревшие имена {found}"
