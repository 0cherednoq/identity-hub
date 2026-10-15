"""Обновить SERVER-ADMINS-PROGRESS.md без ручной правки таблицы.

Примеры:
  python docs/plan/server_admins_progress_tool.py show
  python docs/plan/server_admins_progress_tool.py start S0.1 "Измерение code" "регрессионный тест" "исправление"
  python docs/plan/server_admins_progress_tool.py check "регрессионный тест"
  python docs/plan/server_admins_progress_tool.py note "Тест готов; дальше исправление"
  python docs/plan/server_admins_progress_tool.py done S0.1 - "code у logins.failed · дальше S0.2"
  python docs/plan/server_admins_progress_tool.py status S0.1 blocked "Docker недоступен"
"""

from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path

PATH = Path(__file__).with_name("SERVER-ADMINS-PROGRESS.md")
EMPTY = "- **Задача:** —\n- **Начата:** —\n- **Шаги:**\n  - [ ] —\n- **Заметки для следующей сессии:** —"
CURRENT = re.compile(
    r"(## Текущая задача\n\n(?:<!--.*?-->\n\n)?)(.*?)(\n\n## Статус задач)",
    re.S,
)
ROW = re.compile(r"^\| (S\d+\.\d+[a-z]?) \|.*\|$", re.M)
VALID_STATUSES = {"todo", "in_progress", "done", "blocked", "cancelled"}


def _current_task(text: str) -> str | None:
    match = re.search(r"^- \*\*Задача:\*\* (.+)$", text, re.M)
    if match is None or match.group(1) == "—":
        return None
    return match.group(1).split(maxsplit=1)[0]


def _set_row(
    text: str,
    task: str,
    status: str,
    commit: str | None = None,
    note: str | None = None,
) -> str:
    if status not in VALID_STATUSES:
        allowed = ", ".join(sorted(VALID_STATUSES))
        raise SystemExit(f"неизвестный статус {status!r}; допустимы: {allowed}")

    def repl(match: re.Match[str]) -> str:
        cells = [cell.strip() for cell in match.group(0).strip().strip("|").split("|")]
        cells[3] = status
        if commit is not None:
            cells[4] = commit
        if note is not None:
            cells[5] = note
        return "| " + " | ".join(cells) + " |"

    new, count = re.subn(rf"^\| {re.escape(task)} \|.*\|$", repl, text, flags=re.M)
    if count != 1:
        raise SystemExit(f"задача {task} не найдена или встречается не один раз")
    done = len(re.findall(r"^\| S[^|]+\|[^|]+\|[^|]+\| done \|", new, flags=re.M))
    total = len(ROW.findall(new))
    return re.sub(r"\*\*Итого:\*\* .*", f"**Итого:** {done} / {total} done", new)


def _write(text: str) -> None:
    PATH.write_text(text, encoding="utf-8", newline="\n")


def _require_args(command: str, args: list[str], count: int | None = None) -> None:
    if count is not None and len(args) != count:
        raise SystemExit(f"{command}: ожидалось аргументов: {count}; получено: {len(args)}")


def main(argv: list[str]) -> None:
    if not argv:
        raise SystemExit(__doc__)
    command, *args = argv
    text = PATH.read_text(encoding="utf-8")
    today = dt.date.today().isoformat()

    if command == "show":
        _require_args(command, args, 0)
        current = _current_task(text) or "—"
        summary = re.search(r"\*\*Итого:\*\* (.+)", text)
        print(f"Текущая задача: {current}")
        print(f"Итого: {summary.group(1) if summary else 'не найдено'}")
        return

    if command == "start":
        if len(args) < 3:
            raise SystemExit("start: нужны ID, название и хотя бы один шаг")
        task, title, *steps = args
        if not re.fullmatch(r"S\d+\.\d+[a-z]?", task):
            raise SystemExit(f"неверный ID задачи: {task}")
        active = _current_task(text)
        if active is not None:
            raise SystemExit(f"уже выполняется {active}")
        row = re.search(rf"^\| {re.escape(task)} \|.*\|$", text, re.M)
        if row is None or "| todo |" not in row.group(0):
            raise SystemExit(f"задача {task} отсутствует или не имеет статус todo")
        body = (
            f"- **Задача:** {task} {title}\n"
            f"- **Начата:** {today}\n"
            "- **Шаги:**\n"
            + "\n".join(f"  - [ ] {step}" for step in steps)
            + "\n- **Заметки для следующей сессии:** —"
        )
        text = CURRENT.sub(lambda match: match.group(1) + body + match.group(3), text)
        text = _set_row(text, task, "in_progress")
    elif command == "check":
        _require_args(command, args, 1)
        (needle,) = args
        text, count = re.subn(
            rf"- \[ \] ([^\n]*{re.escape(needle)}[^\n]*)",
            r"- [x] \1",
            text,
            count=1,
        )
        if count != 1:
            raise SystemExit("неотмеченный шаг не найден")
    elif command == "note":
        _require_args(command, args, 1)
        (note,) = args
        text, count = re.subn(
            r"(\*\*Заметки для следующей сессии:\*\*) .*",
            lambda match: f"{match.group(1)} {note}",
            text,
            count=1,
        )
        if count != 1:
            raise SystemExit("поле заметок не найдено")
    elif command == "done":
        _require_args(command, args, 3)
        task, commit, log = args
        active = _current_task(text)
        if active != task:
            raise SystemExit(f"текущая задача {active or '—'}, а закрывается {task}")
        unchecked = re.findall(r"^  - \[ \] (?!—$).+", CURRENT.search(text).group(2), re.M)  # type: ignore[union-attr]
        if unchecked:
            raise SystemExit("нельзя закрыть задачу: остались неотмеченные шаги")
        text = CURRENT.sub(lambda match: match.group(1) + EMPTY + match.group(3), text)
        text = _set_row(text, task, "done", None if commit == "-" else commit)
        text = text.rstrip("\n") + f"\n{today} · {task} · done · {log}\n"
    elif command == "status":
        _require_args(command, args, 3)
        task, status, note = args
        text = _set_row(text, task, status, note=note)
    else:
        raise SystemExit(__doc__)

    _write(text)


if __name__ == "__main__":
    main(sys.argv[1:])
