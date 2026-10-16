#!/usr/bin/env bash
# Проверка Definition of Done: автоисправление импортов, формат, линтер, типы, границы импортов, тесты.
set -euo pipefail
cd "$(dirname "$0")/.."
uv run ruff check --fix-only -q .
uv run ruff format -q .
uv run ruff check .
uv run mypy src tests/typing
uv run lint-imports
if [ "${IH_TEST_SHARDS:-0}" = "1" ]; then
  # На общем PostgreSQL один очень длинный pytest-процесс может оставить асинхронное
  # освобождение ресурса рядом с TRUNCATE следующего теста. Новые процессы разрывают этот
  # цикл, сохраняя тот же набор тестов и один job на версию Python.
  if [ "${IH_STORE_COVERAGE:-0}" = "1" ]; then
    IH_STORE_COVERAGE_FILE="$(mktemp)"
    export IH_STORE_COVERAGE_FILE
    trap 'rm -f "$IH_STORE_COVERAGE_FILE"' EXIT
  fi
  IH_STORE_COVERAGE_FINAL=0 uv run pytest -m "not load" -q -rfE "$@" tests/conformance/test_[a-o]*.py
  IH_STORE_COVERAGE_FINAL=0 uv run pytest -m "not load" -q -rfE "$@" tests/conformance/test_[p-z]*.py
  IH_STORE_COVERAGE_FINAL=1 uv run pytest -m "not load" -q -rfE "$@" \
    tests/pg tests/unit tests/test_*.py
else
  uv run pytest -m "not load" -q -rfE "$@"
fi
