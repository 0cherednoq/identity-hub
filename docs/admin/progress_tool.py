"""Управляет автономным прогрессом Identity Hub Admin.

Примеры:
    python docs/admin/progress_tool.py next
    python docs/admin/progress_tool.py start A0.1 "проверить контракты" "написать отчёт"
    python docs/admin/progress_tool.py check "проверить контракты"
    python docs/admin/progress_tool.py note "продолжить с ..."
    python docs/admin/progress_tool.py done A0.1 - "аудит готов · дальше A0.2"
    python docs/admin/progress_tool.py status A0.1 blocked "внешняя причина"
    python docs/admin/progress_tool.py summary
    python docs/admin/progress_tool.py validate
"""

from __future__ import annotations

import datetime as dt
import re
import sys
from dataclasses import dataclass
from pathlib import Path

PATH = Path(__file__).with_name("PROGRESS.md")
PLAN_PATH = Path(__file__).with_name("PLAN.md")
LIBRARY_PATH = PATH.parent.parent / "plan" / "PROGRESS.md"
VALID_STATUSES = {"todo", "in_progress", "done", "blocked", "cancelled"}
EMPTY_CURRENT = (
    "- **Задача:** —\n- **Начата:** —\n- **Шаги:**\n  - [ ] —\n- **Заметки для следующей сессии:** —"
)
CURRENT_RE = re.compile(
    r"(## Текущая задача\n\n(?:<!--.*?-->\n\n)?)(.*?)(\n\n## Внешние ворота)",
    re.S,
)
ROW_RE = re.compile(r"^\| (?P<id>[AP]\d+(?:\.\d+)?[a-z]?)\s*\|.*\|$", re.M)


@dataclass(frozen=True)
class Task:
    task_id: str
    title: str
    dependencies: tuple[str, ...]
    status: str
    commit: str
    note: str


def _read(path: Path = PATH) -> str:
    return path.read_text(encoding="utf-8")


def _write(text: str) -> None:
    PATH.write_text(text, encoding="utf-8", newline="\n")


def _parse(text: str) -> dict[str, Task]:
    tasks: dict[str, Task] = {}
    for match in ROW_RE.finditer(text):
        cells = [cell.strip() for cell in match.group(0).strip().strip("|").split("|")]
        if len(cells) != 6:
            continue
        task_id, title, dependencies_cell, status, commit, note = cells
        dependencies = tuple(
            value.strip() for value in dependencies_cell.split(",") if value.strip() and value.strip() != "—"
        )
        tasks[task_id] = Task(
            task_id=task_id,
            title=title,
            dependencies=dependencies,
            status=status,
            commit=commit,
            note=note,
        )
    return tasks


def _current_id(text: str) -> str | None:
    match = re.search(r"^- \*\*Задача:\*\* (.+)$", text, re.M)
    if not match or match.group(1).strip() == "—":
        return None
    return match.group(1).strip().split(maxsplit=1)[0]


def _library_statuses() -> dict[str, str]:
    if not LIBRARY_PATH.exists():
        return {}
    statuses: dict[str, str] = {}
    for line in _read(LIBRARY_PATH).splitlines():
        match = re.match(r"^\| (P\d+(?:\.\d+)?[a-z]?) \|.*?\| (\w+) \|", line)
        if match:
            statuses[match.group(1)] = match.group(2)
    return statuses


def _set_row(
    text: str,
    task_id: str,
    status: str,
    commit: str | None = None,
    note: str | None = None,
) -> str:
    if status not in VALID_STATUSES:
        raise SystemExit(f"недопустимый статус: {status}")

    def replace(match: re.Match[str]) -> str:
        cells = [cell.strip() for cell in match.group(0).strip().strip("|").split("|")]
        cells[3] = status
        if commit is not None:
            cells[4] = commit
        if note is not None:
            cells[5] = note
        return "| " + " | ".join(cells) + " |"

    pattern = rf"^\| {re.escape(task_id)} \|.*\|$"
    updated, count = re.subn(pattern, replace, text, count=1, flags=re.M)
    if count != 1:
        raise SystemExit(f"задача {task_id} не найдена")
    return _update_total(updated)


def _update_total(text: str) -> str:
    tasks = _parse(text)
    admin_tasks = [task for task in tasks.values() if task.task_id.startswith("A")]
    done = sum(task.status == "done" for task in admin_tasks)
    return re.sub(r"\*\*Итого:\*\* .*", f"**Итого:** {done} / {len(admin_tasks)} done", text)


def _replace_current(text: str, body: str) -> str:
    updated, count = CURRENT_RE.subn(lambda match: match.group(1) + body + match.group(3), text)
    if count != 1:
        raise SystemExit("раздел текущей задачи не найден")
    return updated


def _unmet_dependencies(task: Task, tasks: dict[str, Task], library: dict[str, str]) -> list[str]:
    unmet: list[str] = []
    for dependency in task.dependencies:
        if dependency.startswith("A"):
            if dependency not in tasks or tasks[dependency].status != "done":
                unmet.append(dependency)
        elif dependency.startswith("P"):
            if library.get(dependency) != "done":
                unmet.append(dependency)
        else:
            unmet.append(dependency)
    return unmet


def _validate(text: str) -> list[str]:
    errors: list[str] = []
    tasks = _parse(text)
    admin_tasks = {key: value for key, value in tasks.items() if key.startswith("A")}
    if not admin_tasks:
        errors.append("таблица задач пуста или не распознана")
        return errors

    ids = [match.group("id") for match in ROW_RE.finditer(text) if match.group("id").startswith("A")]
    if len(ids) != len(set(ids)):
        errors.append("есть повторяющиеся ID задач")

    for task in admin_tasks.values():
        if task.status not in VALID_STATUSES:
            errors.append(f"{task.task_id}: неизвестный статус {task.status}")
        for dependency in task.dependencies:
            if dependency.startswith("A") and dependency not in admin_tasks:
                errors.append(f"{task.task_id}: неизвестная зависимость {dependency}")

    library = _library_statuses()
    for task in admin_tasks.values():
        for dependency in task.dependencies:
            if dependency.startswith("P") and dependency not in library:
                errors.append(f"{task.task_id}: неизвестная библиотечная зависимость {dependency}")
            if (
                task.status == "done"
                and dependency.startswith("A")
                and admin_tasks.get(dependency, task).status != "done"
            ):
                errors.append(f"{task.task_id}: done при незавершённой зависимости {dependency}")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str, chain: tuple[str, ...]) -> None:
        if task_id in visiting:
            errors.append("цикл зависимостей: " + " -> ".join((*chain, task_id)))
            return
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in admin_tasks[task_id].dependencies:
            if dependency in admin_tasks:
                visit(dependency, (*chain, task_id))
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in admin_tasks:
        visit(task_id, ())

    if PLAN_PATH.exists():
        plan_ids = set(re.findall(r"^#### (A\d+\.\d+)\.", _read(PLAN_PATH), re.M))
        table_ids = set(admin_tasks)
        if plan_ids != table_ids:
            missing_in_plan = sorted(table_ids - plan_ids)
            missing_in_table = sorted(plan_ids - table_ids)
            if missing_in_plan:
                errors.append("нет разделов в PLAN.md: " + ", ".join(missing_in_plan))
            if missing_in_table:
                errors.append("нет строк в PROGRESS.md: " + ", ".join(missing_in_table))

    current = _current_id(text)
    in_progress = [task.task_id for task in admin_tasks.values() if task.status == "in_progress"]
    if len(in_progress) > 1:
        errors.append("одновременно отмечено несколько in_progress")
    if current is None and in_progress:
        errors.append(f"таблица содержит in_progress {in_progress[0]}, но текущая задача пуста")
    if current is not None:
        if current not in admin_tasks:
            errors.append(f"текущая задача {current} отсутствует в таблице")
        elif admin_tasks[current].status != "in_progress":
            errors.append(f"текущая задача {current} не имеет статус in_progress")
        if in_progress and current != in_progress[0]:
            errors.append("текущая задача не совпадает с in_progress в таблице")

    total_match = re.search(r"\*\*Итого:\*\* (\d+) / (\d+) done", text)
    actual_done = sum(task.status == "done" for task in admin_tasks.values())
    if not total_match:
        errors.append("строка Итого отсутствует или имеет неверный формат")
    elif (int(total_match.group(1)), int(total_match.group(2))) != (actual_done, len(admin_tasks)):
        errors.append("строка Итого не совпадает с таблицей")
    return errors


def main(argv: list[str]) -> None:
    if not argv:
        raise SystemExit(__doc__)
    command, *args = argv
    text = _read()
    tasks = _parse(text)

    if command == "validate":
        errors = _validate(text)
        if errors:
            raise SystemExit("\n".join(f"ERROR: {error}" for error in errors))
        print(f"OK: {len([task for task in tasks if task.startswith('A')])} задач, структура корректна")
        return

    if command == "summary":
        counts = {status: 0 for status in VALID_STATUSES}
        for task in tasks.values():
            if task.task_id.startswith("A") and task.status in counts:
                counts[task.status] += 1
        print(" ".join(f"{status}={counts[status]}" for status in sorted(counts)))
        current = _current_id(text)
        print(f"current={current or '—'}")
        return

    if command == "next":
        if _current_id(text):
            raise SystemExit(f"сначала завершите текущую задачу {_current_id(text)}")
        library = _library_statuses()
        blocked: list[str] = []
        for task in tasks.values():
            if not task.task_id.startswith("A") or task.status != "todo":
                continue
            unmet = _unmet_dependencies(task, tasks, library)
            if not unmet:
                print(f"{task.task_id} {task.title}")
                return
            blocked.append(f"{task.task_id}: {', '.join(unmet)}")
        detail = "\n".join(blocked[:10])
        raise SystemExit(f"доступных todo-задач нет\n{detail}")

    if command == "start":
        if not 4 <= len(args) <= 9:
            raise SystemExit("start: нужны ID и от 3 до 8 шагов")
        task_id, *steps = args
        if _current_id(text):
            raise SystemExit(f"уже есть текущая задача {_current_id(text)}")
        task = tasks.get(task_id)
        if task is None or not task_id.startswith("A"):
            raise SystemExit(f"задача {task_id} не найдена")
        if task.status != "todo":
            raise SystemExit(f"задача {task_id} имеет статус {task.status}, ожидался todo")
        unmet = _unmet_dependencies(task, tasks, _library_statuses())
        if unmet:
            raise SystemExit(f"не завершены зависимости: {', '.join(unmet)}")
        today = dt.date.today().isoformat()
        body = (
            f"- **Задача:** {task.task_id} {task.title}\n"
            f"- **Начата:** {today}\n"
            "- **Шаги:**\n"
            + "\n".join(f"  - [ ] {step}" for step in steps)
            + "\n- **Заметки для следующей сессии:** —"
        )
        text = _replace_current(text, body)
        _write(_set_row(text, task_id, "in_progress"))
        return

    if command == "check":
        if len(args) != 1:
            raise SystemExit("check: нужна одна подстрока шага")
        needle = args[0]
        updated, count = re.subn(
            rf"^(\s*)- \[ \] ([^\n]*{re.escape(needle)}[^\n]*)$",
            r"\1- [x] \2",
            text,
            count=1,
            flags=re.M,
        )
        if count != 1:
            raise SystemExit("незавершённый шаг с такой подстрокой не найден")
        _write(updated)
        return

    if command == "note":
        if len(args) != 1:
            raise SystemExit("note: нужен один текст заметки")
        if not _current_id(text):
            raise SystemExit("нет текущей задачи")
        updated = re.sub(
            r"(\*\*Заметки для следующей сессии:\*\*) .*",
            lambda match: f"{match.group(1)} {args[0]}",
            text,
            count=1,
        )
        _write(updated)
        return

    if command == "done":
        if len(args) != 3:
            raise SystemExit("done: нужны ID, commit|- и текст журнала")
        task_id, commit, log = args
        if _current_id(text) != task_id:
            raise SystemExit(f"текущая задача не {task_id}")
        current_match = CURRENT_RE.search(text)
        if current_match and re.search(r"^\s*- \[ \] (?!—$).+", current_match.group(2), re.M):
            raise SystemExit("нельзя завершить: остались неотмеченные шаги")
        text = _replace_current(text, EMPTY_CURRENT)
        text = _set_row(text, task_id, "done", None if commit == "-" else commit)
        today = dt.date.today().isoformat()
        text = text.rstrip() + f"\n{today} · {task_id} · done · {log}\n"
        errors = _validate(text)
        if errors:
            raise SystemExit("\n".join(errors))
        _write(text)
        return

    if command == "status":
        if len(args) != 3:
            raise SystemExit("status: нужны ID, статус и примечание")
        task_id, status, note = args
        if status == "in_progress":
            raise SystemExit("для in_progress используйте start")
        if task_id not in tasks or not task_id.startswith("A"):
            raise SystemExit(f"задача {task_id} не найдена")
        if _current_id(text) == task_id:
            text = _replace_current(text, EMPTY_CURRENT)
        updated = _set_row(text, task_id, status, note=note)
        errors = _validate(updated)
        if errors:
            raise SystemExit("\n".join(errors))
        _write(updated)
        return

    raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
