"""C4.3: состав `data` событий совпадает с таблицей в `docs/guide/10-events.md`.

Плагин оборачивает `SpaceBase._emit` на всю сессию тестов. Событие, которое записал код
библиотеки, должно иметь в `data` только ключи, перечисленные для его типа в руководстве:
новый ключ без правки документации роняет тест, в котором он появился. События, которые тест
пишет сам (`space._emit(...)` из каталога `tests`), не проверяются.

`IH_RECORD_EVENT_DATA=<файл>` вместо проверки записывает встреченные ключи в JSON — так
таблицу сверяют после изменения событий.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from identity_hub._space import SpaceBase

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs" / "guide" / "10-events.md"
TESTS = str(ROOT / "tests")
START, END = "<!-- event-data:start -->", "<!-- event-data:end -->"
_ROW = re.compile(r"^\| `([a-z_.]+)` \| (.*?) \|$", re.M)


def documented() -> dict[str, set[str]]:
    """Тип события → ключи `data` из таблицы руководства."""
    text = GUIDE.read_text(encoding="utf-8")
    table = text[text.index(START) : text.index(END)]
    return {kind: set(re.findall(r"`([A-Za-z_]+)`", keys)) for kind, keys in _ROW.findall(table)}


@pytest.fixture(scope="session", autouse=True)
def _event_data_contract() -> Iterator[None]:
    record_to = os.environ.get("IH_RECORD_EVENT_DATA")
    allowed = {} if record_to else documented()
    seen: dict[str, set[str]] = {}
    original = SpaceBase._emit

    async def checked(self: SpaceBase, tx: Any, *drafts: Any) -> Any:
        if not sys._getframe(1).f_code.co_filename.startswith(TESTS):
            for draft in drafts:
                keys = set(draft.data)
                seen.setdefault(draft.type, set()).update(keys)
                extra = keys - allowed.get(draft.type, set())
                if extra and not record_to:
                    raise AssertionError(
                        f"событие {draft.type}: ключи data {sorted(extra)} не описаны в 10-events.md"
                    )
        return await original(self, tx, *drafts)

    SpaceBase._emit = checked  # type: ignore[method-assign]
    try:
        yield
    finally:
        SpaceBase._emit = original  # type: ignore[method-assign]
        if record_to:
            Path(record_to).write_text(
                json.dumps(
                    {kind: sorted(keys) for kind, keys in sorted(seen.items())}, ensure_ascii=False, indent=1
                ),
                encoding="utf-8",
            )
