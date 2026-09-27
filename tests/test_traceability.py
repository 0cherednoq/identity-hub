"""P10.5: матрица содержит каждый нормативный ID и реальные pytest nodes."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).parents[1]
REQUIREMENTS = ROOT / "identity-hub-requirements.md"
MATRIX = ROOT / "docs" / "traceability.md"
REQUIREMENT_ID = re.compile(r"\*\*((?:FR|G)-\d+\.\d+|(?:T|S)-\d+)\.\*\*")
MATRIX_ROW = re.compile(r"^\| ((?:FR|G)-\d+\.\d+|(?:T|S)-\d+) \| (.+) \|$", re.MULTILINE)
PYTEST_NODE = re.compile(r"`(tests/[^`:]+\.py)::(test_[^`]+)`")


def test_traceability_has_every_requirement_exactly_once() -> None:
    expected = set(REQUIREMENT_ID.findall(REQUIREMENTS.read_text(encoding="utf-8")))
    rows = MATRIX_ROW.findall(MATRIX.read_text(encoding="utf-8"))
    counts = Counter(requirement for requirement, _ in rows)

    assert len(expected) == 121
    assert set(counts) == expected
    assert all(count == 1 for count in counts.values())


def test_every_traceability_row_references_existing_pytest_node() -> None:
    for requirement, evidence in MATRIX_ROW.findall(MATRIX.read_text(encoding="utf-8")):
        nodes = PYTEST_NODE.findall(evidence)
        assert nodes, f"{requirement}: нет pytest node"
        for relative, test_name in nodes:
            path = ROOT / relative
            assert path.is_file(), f"{requirement}: нет {relative}"
            source = path.read_text(encoding="utf-8")
            assert re.search(rf"(?:async )?def {re.escape(test_name)}\(", source), (
                f"{requirement}: нет {test_name} в {relative}"
            )
