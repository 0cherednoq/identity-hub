#!/usr/bin/env bash
# Проверка Definition of Done: автоисправление импортов, формат, линтер, типы, границы импортов, тесты.
set -euo pipefail
cd "$(dirname "$0")/.."
uv run ruff check --fix-only -q .
uv run ruff format -q .
uv run ruff check .
uv run mypy src
uv run lint-imports
uv run pytest -m "not load" -q -rfE "$@"
