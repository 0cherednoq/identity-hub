"""I0.1: матрица очереди улучшений согласована с планом и прогрессом."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).parents[1]
MATRIX = ROOT / "docs" / "improvements-traceability.md"
PLAN = ROOT / "docs" / "plan" / "IMPROVEMENTS-PLAN.md"
PROGRESS = ROOT / "docs" / "plan" / "IMPROVEMENTS-PROGRESS.md"

ROW = re.compile(r"^\| (IMP-\d{2}) \| [^|]+ \| ([^|]+) \| (\w+) \| (.+) \|$", re.MULTILINE)
TASK = re.compile(r"I\d+\.\d+[a-z]?")
TASK_RANGE = re.compile(r"(I(\d+))\.(\d+)–I\2\.(\d+)")
PYTEST_NODE = re.compile(r"`(tests/[^`:]+\.py)::(test_[^`]+)`")
STATES = {"missing", "partial", "implemented", "deferred"}
ACTIVE = {f"IMP-{n:02d}" for n in (*range(1, 22), 23)}


def _rows() -> list[tuple[str, str, str, str]]:
    return ROW.findall(MATRIX.read_text(encoding="utf-8"))


def _tasks(cell: str) -> set[str]:
    tasks = set(TASK.findall(cell))
    for prefix, _phase, start, end in TASK_RANGE.findall(cell):
        tasks.update(f"{prefix}.{n}" for n in range(int(start), int(end) + 1))
    return tasks


def test_every_improvement_appears_exactly_once() -> None:
    counts = Counter(imp for imp, *_ in _rows())
    assert set(counts) == ACTIVE | {"IMP-22"}
    assert all(count == 1 for count in counts.values())


def test_only_runtime_profile_is_deferred() -> None:
    states = {imp: state for imp, _tasks, state, _tests in _rows()}
    assert set(states.values()) <= STATES
    assert states["IMP-22"] == "deferred"
    assert all(states[imp] != "deferred" for imp in ACTIVE)


def test_matrix_tasks_exist_in_plan_and_progress() -> None:
    plan_tasks = set(re.findall(r"^### (I\d+\.\d+[a-z]?) ", PLAN.read_text(encoding="utf-8"), re.MULTILINE))
    progress_tasks = set(
        re.findall(r"^\| (I\d+\.\d+[a-z]?) \|", PROGRESS.read_text(encoding="utf-8"), re.MULTILINE)
    )
    assert plan_tasks == progress_tasks
    for imp, cell, _state, _tests in _rows():
        tasks = _tasks(cell)
        if imp in ACTIVE:
            assert tasks, f"{imp}: нет задач"
        assert tasks <= plan_tasks, f"{imp}: неизвестные задачи {tasks - plan_tasks}"


def test_implemented_rows_reference_existing_tests() -> None:
    for imp, _tasks_cell, state, tests in _rows():
        if state != "implemented":
            continue
        assert "planned:" not in tests, f"{imp}: implemented с планируемыми тестами"
        nodes = PYTEST_NODE.findall(tests)
        assert nodes, f"{imp}: нет pytest node"
        for relative, name in nodes:
            source = (ROOT / relative).read_text(encoding="utf-8")
            assert re.search(rf"(?:async )?def {re.escape(name)}\(", source), f"{imp}: нет {name}"
