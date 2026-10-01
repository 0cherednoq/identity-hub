"""F0.1: находки ревью перед публикацией согласованы с планом исправлений и прогрессом."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).parents[1]
REVIEW = ROOT / "docs" / "review" / "PRE-RELEASE-REVIEW.md"
PLAN = ROOT / "docs" / "plan" / "RELEASE-FIXES-PLAN.md"
PROGRESS = ROOT / "docs" / "plan" / "RELEASE-FIXES-PROGRESS.md"
IMPROVEMENTS_PLAN = ROOT / "docs" / "plan" / "IMPROVEMENTS-PLAN.md"

FINDING = re.compile(r"\bR1-\d{2}\b|\bR2-[SFDP]\d{2}\b")
MATRIX_ROW = re.compile(r"^\| (R[12]-[SFDP]?\d{2}) \| ([FI]\d+\.\d+[a-z]?) \|$", re.MULTILINE)
MATRIX_HEADING = "## 9. Матрица трассировки"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _matrix() -> list[tuple[str, str]]:
    return MATRIX_ROW.findall(_read(PLAN).split(MATRIX_HEADING, maxsplit=1)[1])


def _headings(path: Path, prefix: str) -> set[str]:
    return set(re.findall(rf"^### ({prefix}\d+\.\d+[a-z]?) ", _read(path), re.MULTILINE))


def test_every_review_finding_is_mapped_exactly_once() -> None:
    findings = set(FINDING.findall(_read(REVIEW)))
    counts = Counter(finding for finding, _task in _matrix())
    assert findings, "в ревью не найдено ни одной находки"
    assert set(counts) == findings
    assert [finding for finding, count in counts.items() if count != 1] == []


def test_matrix_tasks_exist_in_their_plans() -> None:
    fix_tasks = _headings(PLAN, "F")
    improvement_tasks = _headings(IMPROVEMENTS_PLAN, "I")
    for finding, task in _matrix():
        known = fix_tasks if task.startswith("F") else improvement_tasks
        assert task in known, f"{finding}: неизвестная задача {task}"


def test_plan_and_progress_list_the_same_tasks() -> None:
    progress_tasks = re.findall(r"^\| (F\d+\.\d+[a-z]?) \|", _read(PROGRESS), re.MULTILINE)
    assert len(progress_tasks) == len(set(progress_tasks))
    assert set(progress_tasks) == _headings(PLAN, "F")


def test_task_sections_name_the_findings_they_close() -> None:
    """Раздел задачи перечисляет ровно те находки, что отданы ей матрицей."""
    plan = _read(PLAN).split(MATRIX_HEADING, maxsplit=1)[0]
    sections = re.split(r"^### (F\d+\.\d+[a-z]?) .*$", plan, flags=re.MULTILINE)
    declared: dict[str, set[str]] = {}
    for task, body in zip(sections[1::2], sections[2::2], strict=True):
        line = re.search(r"^Находки: (.+)$", body, re.MULTILINE)
        declared[task] = set(FINDING.findall(line.group(1))) if line else set()
    expected: dict[str, set[str]] = {task: set() for task in declared}
    for finding, task in _matrix():
        if task.startswith("F"):
            expected[task].add(finding)
    assert declared == expected
