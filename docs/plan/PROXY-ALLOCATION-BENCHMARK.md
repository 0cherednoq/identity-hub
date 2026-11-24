# Измерение proxy allocations

Дата: 2026-11-24. Это локальный baseline перед дальнейшей оптимизацией, не SLA библиотеки.
Измеритель: [proxy_allocations.py](../../benchmarks/proxy_allocations.py).
План: [PA5.1](PROXY-ALLOCATION-PLAN.md#10-проверки-и-матрица-гонок).

Сравниваются исходный Git HEAD `5ba79bddf56964f8508e29285e85a754297f0065` (schema 0043),
текущее дерево в bindings и текущее дерево в allocations (schema 0048).
HEAD загружен из git archive в отдельном процессе; измеритель проверяет путь реально
импортированного пакета. Каждый вариант использует новую схему одной локальной PostgreSQL БД.
Прогоны последовательные, без параллельных pytest. Windows 11, Python 3.14.3, 16 логических CPU.
50 работников, 25 измерений после прогрева; один proxy вместимостью 50, 49 удерживаемых use.
Provider network I/O отсутствует. SQL statements считаются через SQLAlchemy; BEGIN/COMMIT не
учтены, это показатель числа SQL-команд, а не точный подсчёт всех пакетов сетевого протокола.

## Результаты первого прогона

В ячейках задержки: p50 / p95 в миллисекундах. SQL — команд на один вызов.

| Сценарий | HEAD, задержка; SQL | Текущий bindings, задержка; SQL | Allocations, задержка; SQL |
| --- | --- | --- | --- |
| Acquire существующей пары при полной ёмкости | 30.7 / 40.1; 11 | 53.1 / 72.7; 20 | 102.5 / 187.0; 53 |
| Release существующей пары | 24.5 / 32.8; 8 | 24.9 / 34.2; 9 | 45.2 / 65.3; 20 |
| Acquire временного use при высокой занятости | 28.3 / 36.9; 12 | 55.9 / 110.9; 21 | 77.5 / 105.3; 41 |
| Release временного use | 19.4 / 26.3; 8 | 24.4 / 38.6; 9 | 56.7 / 86.4; 30 |
| Heartbeat одного use | 8.4 / 19.1; 1 | 6.6 / 8.5; 1 | 8.5 / 10.4; 1 |
| Heartbeat 49 use одной пачкой | 8.8 / 32.6; 1 | 7.9 / 9.4; 1 | 10.7 / 28.6; 1 |
| 50 конкурентов за последнее место | 361.7 / 423.8; 7.18 | 1396.5 / 1867.8; 23.80 | 1353.7 / 1994.5; 34.88 |

Каждый вариант дал одного победителя и 49 отказов за последнее место. Среднее число SQL
в contention зависит от конкретного пересечения транзакций и не является постоянной константой.
Длительности от одного локального прогона шумные; более быстрый heartbeat bindings не доказывает
ускорения алгоритма. Поведение временного use различается намеренно: legacy sticky=False не
занимает физическое место, allocations deferred резервирует его.
Поле throughput конкурентного профиля в первых JSON считалось по сумме задержек и здесь не
используется. Измеритель исправлен: дальнейшие отчёты считают число попыток за wall time;
сохранённые p50/p95, SQL, locks и размеры индексов от этой поправки не меняются.

| Профиль | HEAD | Текущий bindings | Allocations |
| --- | --- | --- | --- |
| Expiry, batch 8: время / SQL | 124.4 ms / 36 | 167.2 ms / 74 | 943.5 ms / 315 |
| Expiry, batch 32: время / SQL | 240.5 ms / 108 | 378.6 ms / 217 | 1765.6 ms / 749 |
| Drain: unbind и попытка чужой выдачи | 72.6 ms / 22 SQL | 74.3 ms / 40 SQL | 196.3 ms / 60 SQL |
| Повторная выдача при ещё живом владельце | Да, прежний контракт | Да, прежний контракт | Отказ до release |
| Сумма размеров индексов тестовой схемы | 1 900 544 B | 2 080 768 B | 2 318 336 B |
| Peak ожидающих locks при sampling ≥10 ms | 48 | 49 | 49 |
| Peak удерживаемых locks | 3035 | 3605 | 4357 |
| Deadlocks за прогон | 0 | 0 | 0 |

После накопленных expiry все 50 потерянных use перестали считаться живыми; strict occupied=0.
Проходы используют разные следующие пачки одного backlog, поэтому время batch 32 нельзя
трактовать как сравнение с batch 8 на идентичном наборе строк. Bounded SQL plans и пропуск занятых
строк отдельно проверяются `test_proxy_recovery_plans.py` и `test_proxy_recovery_locks.py`.
Размер индексов включает все таблицы схемы и последствия сценариев; это не размер одной
reservation и не оценка production retention.

## Бюджеты для следующих изменений

Зафиксированы по первым измерениям до последующей оптимизации. Для детерминированных
последовательных профилей верхние границы SQL: HEAD 11/8 и 12/8, bindings 20/9 и 21/9,
allocations 53/20 и 41/30 (acquire/release существующего и временного назначения).
Новый запрос сверх этих значений требует отдельного объяснения и повторного сравнения.
Для heartbeat строгий бюджет — один SQL на пачку, независимо от числа её use;
измеритель явно проверяет и SQL, и число продлённых аренд. Для correctness бюджеты нулевые:
переполнение, второй победитель, deadlock и потерянное место после полного cleanup недопустимы.
Discovery/lock attempts recovery ограничены переданным limit, acquire retry — 20.

Численные latency-пороги существующего `tests/load` не менялись. Измеренный рост числа SQL
и задержки acquire — известная цена нового пути и основание для будущего уменьшения round trips.
Полный load-прогон учитывается отдельно: его исторический PERF-1 xfail не считается выполненным SLA.
Уменьшение SQL не должно обходить lock order, fence, group revision, audit или общую finalization.

Исходные отчёты текущей рабочей копии: `test-results/pa5-benchmark-baseline.json`,
`pa5-benchmark-legacy.json`, `pa5-benchmark-allocations.json`. Каталог игнорируется Git;
эта таблица сохраняет проверяемые значения в документации. Первая попытка baseline завершила
сценарии, но потеряла отчёт из-за занятого единственного observer connection. Измеритель исправлен
и baseline повторён на новой схеме; в таблице только успешно завершённый повтор.

Воспроизведение — последовательно, с новыми tag и тестовым IH_TEST_DATABASE_URL:

```powershell
git archive --format=zip --output=test-results/pa5-baseline-src.zip HEAD src/identity_hub
.venv/Scripts/python.exe -m zipfile -e test-results/pa5-baseline-src.zip test-results/pa5-baseline-src
.venv/Scripts/python.exe benchmarks/proxy_allocations.py --variant baseline --tag repeat1 --source test-results/pa5-baseline-src/src --output test-results/baseline.json
.venv/Scripts/python.exe benchmarks/proxy_allocations.py --variant legacy --tag repeat1 --output test-results/legacy.json
.venv/Scripts/python.exe benchmarks/proxy_allocations.py --variant allocations --tag repeat1 --output test-results/allocations.json
```

Измеритель создаёт собственные схемы, отклоняет занятый tag и оставляет данные для проверки.
Для повторения зафиксированного baseline архивировать нужно указанный commit, если HEAD изменился.

Повтор окончательного измерителя на трёх новых схемах завершился с exit=0. Артефакты —
`test-results/pa5-benchmark-{baseline,legacy,allocations}-verified.json`. Все последовательные
SQL-бюджеты совпали с таблицей; assertions подтвердили один SQL на heartbeat-пачку и продление
всех её use. Снова получены один winner/49 refusals, deadlocks=0, корректный strict drain и
occupied=0 после cleanup. Конкурентный throughput теперь измерен по wall time: HEAD 79.83,
bindings 40.47, allocations 25.61 попытки/с. Это включает отказы, не число успешно выданных аренд.
