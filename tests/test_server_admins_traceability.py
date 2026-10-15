"""S4.1: ишью server-admins, план и прогресс согласованы."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
PLAN = (ROOT / "docs" / "plan" / "SERVER-ADMINS-PLAN.md").read_text(encoding="utf-8")
PROGRESS = (ROOT / "docs" / "plan" / "SERVER-ADMINS-PROGRESS.md").read_text(encoding="utf-8")
ISSUES = ROOT / "docs" / "isue" / "server-admins"

TASK = re.compile(r"S\d+\.\d+")
MATRIX_ROW = re.compile(r"^\| (\d+)\. [^|]+ \| ([^|]+) \|$", re.MULTILINE)
PROGRESS_ROW = re.compile(r"^\| (S\d+\.\d+) \| [^|]+ \| [^|]* \| (\w+) \|", re.MULTILINE)
STATUSES = {"todo", "in_progress", "done", "blocked", "cancelled"}


def _matrix() -> dict[int, str]:
    table = PLAN[PLAN.index("## 8. Матрица трассировки") :]
    return {int(number): cell for number, cell in MATRIX_ROW.findall(table)}


def _progress() -> dict[str, str]:
    return dict(PROGRESS_ROW.findall(PROGRESS))


def test_every_server_admins_issue_is_traced() -> None:
    issue_numbers = {int(path.name[:2]) for path in ISSUES.glob("[0-9][0-9]-*.md") if path.name[:2].isdigit()}
    matrix = _matrix()
    assert issue_numbers == set(range(1, 9)) == set(matrix)
    for number, cell in matrix.items():
        if number == 8:
            assert cell.strip() == "не выполняется"
        else:
            assert TASK.findall(cell), f"ишью {number}: нет задач"


def test_plan_tasks_match_progress() -> None:
    progress = _progress()
    table = PLAN[PLAN.index("## 8. Матрица трассировки") :]
    traced = set(TASK.findall(table))
    assert traced == set(progress)
    assert set(progress.values()) <= STATUSES
    headings = set(re.findall(r"^### (S\d+\.\d+) ", PLAN, re.MULTILINE))
    assert set(progress) == headings


def test_summary_line_matches_table() -> None:
    progress = _progress()
    done = sum(status == "done" for status in progress.values())
    assert f"**Итого:** {done} / {len(progress)} done" in PROGRESS
