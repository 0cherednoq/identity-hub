"""Утилита обновления PROGRESS.md: статусы задач, текущая задача, журнал.

python docs/plan/progress_tool.py start P0.2 "Название" "шаг 1" "шаг 2" ...
python docs/plan/progress_tool.py check "<подстрока шага>"
python docs/plan/progress_tool.py done P0.2 <commit|-> "<журнал: что сделано · что дальше>"
python docs/plan/progress_tool.py status P0.2 blocked "<примечание>"
"""

from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path

PATH = Path(__file__).with_name("PROGRESS.md")
EMPTY = (
    "- **Задача:** —\n- **Начата:** —\n- **Шаги:**\n  - [ ] —\n- **Заметки для следующей сессии:** —"
)
CURRENT = re.compile(r"(## Текущая задача\n\n(?:<!--.*?-->\n\n)?)(.*?)(\n\n## Статус задач)", re.S)


def _set_row(text: str, task: str, status: str, commit: str | None = None, note: str | None = None) -> str:
    def repl(m: re.Match[str]) -> str:
        cells = [c.strip() for c in m.group(0).strip().strip("|").split("|")]
        cells[3] = status
        if commit is not None:
            cells[4] = commit
        if note is not None:
            cells[5] = note
        return "| " + " | ".join(cells) + " |"

    new, n = re.subn(rf"^\| {re.escape(task)} \|.*\|$", repl, text, flags=re.M)
    if n != 1:
        raise SystemExit(f"задача {task} не найдена")
    done = len(re.findall(r"^\| P[^|]+\|[^|]+\|[^|]+\| done \|", new, flags=re.M))
    total = len(re.findall(r"^\| P\d", new, flags=re.M))
    return re.sub(r"\*\*Итого:\*\* .*", f"**Итого:** {done} / {total} done", new)


def main(argv: list[str]) -> None:
    text = PATH.read_text(encoding="utf-8")
    cmd, *args = argv
    today = dt.date.today().isoformat()
    if cmd == "start":
        task, title, *steps = args
        body = (
            f"- **Задача:** {task} {title}\n- **Начата:** {today}\n- **Шаги:**\n"
            + "\n".join(f"  - [ ] {s}" for s in steps)
            + "\n- **Заметки для следующей сессии:** —"
        )
        text = CURRENT.sub(lambda m: m.group(1) + body + m.group(3), text)
        text = _set_row(text, task, "in_progress")
    elif cmd == "check":
        (needle,) = args
        text, n = re.subn(rf"- \[ \] ([^\n]*{re.escape(needle)})", r"- [x] \1", text, count=1)
        if not n:
            raise SystemExit("шаг не найден")
    elif cmd == "note":
        (note,) = args
        text = re.sub(r"(\*\*Заметки для следующей сессии:\*\*) .*", lambda m: f"{m.group(1)} {note}", text)
    elif cmd == "done":
        task, commit, log = args
        text = CURRENT.sub(lambda m: m.group(1) + EMPTY + m.group(3), text)
        text = _set_row(text, task, "done", None if commit == "-" else commit)
        text = text.rstrip("\n") + f"\n{today} · {task} · done · {log}\n"
    elif cmd == "status":
        task, status, note = args
        text = _set_row(text, task, status, note=note)
    else:
        raise SystemExit(__doc__)
    PATH.write_text(text, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main(sys.argv[1:])
