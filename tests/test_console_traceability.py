"""C7.1: очередь доработок по запросам пульта согласована — ишью, план, прогресс."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
PLAN = (ROOT / "docs" / "plan" / "CONSOLE-PLAN.md").read_text(encoding="utf-8")
PROGRESS = (ROOT / "docs" / "plan" / "CONSOLE-PROGRESS.md").read_text(encoding="utf-8")
REJECTED = (ROOT / "docs" / "plan" / "CONSOLE-REJECTED.md").read_text(encoding="utf-8")
ISSUES = (ROOT / "docs" / "isue" / "ISSUES-identity-hub.md").read_text(encoding="utf-8")

TASK = re.compile(r"C\d+\.\d+[a-z]?")
MATRIX_ROW = re.compile(r"^\| (\d+)\. [^|]+ \| ([^|]+) \|$", re.MULTILINE)
PROGRESS_ROW = re.compile(r"^\| (C\d+\.\d+[a-z]?) \| [^|]+ \| [^|]* \| (\w+) \|", re.MULTILINE)
STATUSES = {"todo", "in_progress", "done", "blocked", "cancelled"}
NOT_DONE = {10, 11, 12, 14}  # отклонены целиком, причины — в CONSOLE-REJECTED.md


def _matrix() -> dict[int, str]:
    table = PLAN[PLAN.index("## 8. Матрица трассировки") :]
    return {int(number): cell for number, cell in MATRIX_ROW.findall(table)}


def _progress() -> dict[str, str]:
    return dict(PROGRESS_ROW.findall(PROGRESS))


def test_every_issue_item_is_traced() -> None:
    """Каждый пункт ишью либо ведёт к задачам плана, либо отклонён с записанной причиной."""
    issue_items = {int(n) for n in re.findall(r"^## (\d+)\. ", ISSUES, re.MULTILINE)}
    matrix = _matrix()
    assert issue_items == set(range(1, 15)) == set(matrix)
    for number, cell in matrix.items():
        if number in NOT_DONE:
            assert cell.strip() == "не выполняется"
            assert f"### Пункт {number}." in REJECTED, f"пункт {number}: нет причины отказа"
        else:
            assert TASK.findall(cell), f"пункт {number}: нет задач"


def test_matrix_tasks_match_progress() -> None:
    """Задачи матрицы есть в прогрессе, и каждая задача прогресса закрывает пункт или сверку."""
    progress = _progress()
    table = PLAN[PLAN.index("## 8. Матрица трассировки") :]
    traced = set(TASK.findall(table))
    assert traced == set(progress)
    assert set(progress.values()) <= STATUSES
    headings = set(re.findall(r"^### (C\d+\.\d+) ", PLAN, re.MULTILINE))
    assert {re.sub(r"[a-z]$", "", task) for task in progress} == headings


def test_summary_line_matches_table() -> None:
    progress = _progress()
    done = sum(status == "done" for status in progress.values())
    assert f"**Итого:** {done} / {len(progress)} done" in PROGRESS
