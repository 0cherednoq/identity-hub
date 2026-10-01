# Скрипты воспроизведения находок ревью

Разовые скрипты, которыми подтверждались находки из
[`PRE-RELEASE-REVIEW.md`](../PRE-RELEASE-REVIEW.md). Это не тесты: pytest их не собирает, ruff
и mypy их не проверяют. Назначение — исходный материал для регрессионных тестов задач
[`RELEASE-FIXES-PLAN.md`](../../plan/RELEASE-FIXES-PLAN.md).

| Каталог | Раздел ревью | Что нужно для запуска |
|---|---|---|
| `api/` | раздел 1 (R1-01, R1-02, R1-07) | только `.venv`; `plugin_*_mode.py` — pytest в чистом окружении с установленным wheel |
| `secrets/` | 4.1 и 4.2 | `.venv`; части с `--pg` — PostgreSQL, адрес в `common.py` (`PG_URL`) |
| `functional/` | 4.3 | только `.venv` (`MemoryStore`); рядом сохранён вывод `*.out` |
| `schema/` | 4.4 | PostgreSQL 16 и PgBouncer, адреса и порт в `sch_common.py` |
| `parity/` | 4.5 | PostgreSQL, адрес в переменной `IH_PARITY_PG_URL` |

Запуск из корня репозитория:

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe docs/review/repro/functional/t_login.py
```

Скрипты писались под временные контейнеры: адреса, порты и учётные данные БД в них тестовые и
больше не существуют — перед запуском подставьте свои. После исправления находки скрипт должен
показывать новое поведение; когда находка покрыта тестом в `tests/`, скрипт можно удалить.
