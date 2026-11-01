"""AR4.1: гарантии перепроверки аккаунта ссылаются на существующие pytest nodes."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).parents[1]
MATRIX = ROOT / "docs" / "auth-revalidation-traceability.md"
ROW = re.compile(r"^\| (AR-R\d{2}) \| [^|]+ \| `([^`:]+\.py)::(test_[^`]+)` \|$", re.MULTILINE)


def test_auth_revalidation_requirements_are_unique_and_complete() -> None:
    rows = ROW.findall(MATRIX.read_text(encoding="utf-8"))
    counts = Counter(requirement for requirement, _path, _test in rows)
    assert set(counts) == {f"AR-R{number:02d}" for number in range(1, 24)}
    assert all(count == 1 for count in counts.values())


def test_auth_revalidation_traceability_nodes_exist() -> None:
    for requirement, relative, name in ROW.findall(MATRIX.read_text(encoding="utf-8")):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert re.search(rf"(?:async )?def {re.escape(name)}\(", source), f"{requirement}: нет {name}"
