"""N0.1: план очереди `N`, его матрица и прогресс согласованы с `ISSUES-INTEGRATION.md`."""

from __future__ import annotations

import re
from pathlib import Path

PLAN_DIR = Path(__file__).parents[1] / "docs" / "plan"
PLAN = (PLAN_DIR / "INTEGRATION-PLAN.md").read_text(encoding="utf-8")
PROGRESS = (PLAN_DIR / "INTEGRATION-PROGRESS.md").read_text(encoding="utf-8")
ISSUES = (PLAN_DIR / "ISSUES-INTEGRATION.md").read_text(encoding="utf-8")

TASK = re.compile(r"N\d+\.\d+[a-z]?")
MATRIX_ROW = re.compile(r"^\| (\d)\. [^|]+ \| ([^|]+) \|$", re.MULTILINE)
SKIPPED = {8}  # D-221: сессии-файлы больше 512 KiB не выполняются


def _plan_tasks() -> set[str]:
    return set(re.findall(r"^### (N\d+\.\d+[a-z]?) ", PLAN, re.MULTILINE))


def _matrix() -> dict[int, set[str]]:
    section = PLAN.split("## 7. Матрица трассировки", 1)[1]
    return {int(number): set(TASK.findall(cell)) for number, cell in MATRIX_ROW.findall(section)}


def test_plan_and_progress_list_the_same_tasks() -> None:
    progress_tasks = set(re.findall(r"^\| (N\d+\.\d+[a-z]?) \|", PROGRESS, re.MULTILINE))
    assert _plan_tasks() == progress_tasks


def test_every_issue_is_in_the_matrix() -> None:
    issues = {int(number) for number in re.findall(r"^## (\d)\. ", ISSUES, re.MULTILINE)}
    matrix = _matrix()
    assert set(matrix) == issues
    for number, tasks in matrix.items():
        if number in SKIPPED:
            assert not tasks, f"задача {number} не выполняется, но в матрице есть {tasks}"
        else:
            assert tasks, f"задача {number}: нет задач плана"


def test_matrix_covers_every_plan_task() -> None:
    section = PLAN.split("## 7. Матрица трассировки", 1)[1]
    assert set(TASK.findall(section)) == _plan_tasks()
