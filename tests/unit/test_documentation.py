"""Проверки локальных ссылок в пользовательской документации."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
DOCUMENTS = (
    ROOT / "README.md",
    ROOT / "CHANGELOG.md",
    ROOT / "docs" / "cli.md",
    ROOT / "docs" / "integrations.md",
    ROOT / "docs" / "perf.md",
    ROOT / "docs" / "traceability.md",
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
