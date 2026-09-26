"""Исполняемые проверки пользовательских примеров."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from examples.telegram_service import run_example

ROOT = Path(__file__).parents[2]


@pytest.mark.unit
async def test_telegram_example_runs_public_flow() -> None:
    result = await run_example()

    assert result.messages == ("message-after-0",)
    assert result.saved_cursor == 1
    assert {"identity.registered", "identity.updated"} <= set(result.event_types)


@pytest.mark.unit
def test_telegram_example_is_executable() -> None:
    completed = subprocess.run(
        [sys.executable, "examples/telegram_service.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert "messages=1 saved_cursor=1" in completed.stdout
