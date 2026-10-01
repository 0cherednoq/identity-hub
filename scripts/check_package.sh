#!/usr/bin/env bash
# Проверка собранного пакета: wheel ставится в чистое окружение без исходников репозитория,
# импортируется, запускает CLI и примеры; sdist не содержит лишнего.
# Использование: uv build --out-dir dist && bash scripts/check_package.sh dist
set -euo pipefail
cd "$(dirname "$0")/.."
root="$(pwd)"
dist="$(cd "${1:-dist}" && pwd)"

wheels=("$dist"/identity_hub-*.whl)
sdists=("$dist"/identity_hub-*.tar.gz)
[ -f "${wheels[0]}" ] || { echo "wheel не найден в $dist" >&2; exit 1; }
[ -f "${sdists[0]}" ] || { echo "sdist не найден в $dist" >&2; exit 1; }
[ "${#wheels[@]}" -eq 1 ] && [ "${#sdists[@]}" -eq 1 ] || { echo "в $dist несколько сборок" >&2; exit 1; }

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

uv venv -q "$work/venv"
if [ -x "$work/venv/Scripts/python.exe" ]; then
  bin="$work/venv/Scripts"
  python="$bin/python.exe"
else
  bin="$work/venv/bin"
  python="$bin/python"
fi
uv pip install -q --python "$python" "${wheels[0]}"

# Запуск из пустого каталога: пакет берётся из окружения, а не из src/ репозитория.
cp "$root"/examples/telegram_service.py "$root"/examples/account_lifecycle.py "$work/"
cd "$work"
"$python" - <<'PY'
import pathlib

import identity_hub

location = pathlib.Path(identity_hub.__file__).resolve()
assert "site-packages" in location.parts, f"импортирован не установленный пакет: {location}"
assert (location.parent / "py.typed").is_file(), "в wheel нет py.typed"
assert (location.parent / "postgres" / "migrations" / "versions").is_dir(), "в wheel нет миграций"
print("identity_hub", identity_hub.__version__, "из", location.parent)
PY
"$bin/identity-hub" --help > /dev/null
"$python" telegram_service.py > /dev/null
"$python" account_lifecycle.py > /dev/null

# sdist — только библиотека (см. [tool.hatch.build.targets.sdist]).
if tar -tzf "${sdists[0]}" | grep -E '^[^/]+/(admin|docs/plan|docs/review|\.github|\.venv)/' ; then
  echo "sdist содержит файлы, не относящиеся к библиотеке" >&2
  exit 1
fi
echo "пакет проверен: ${wheels[0]##*/}, ${sdists[0]##*/}"
