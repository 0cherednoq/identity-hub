# Прогресс имплементации Identity Hub v1

> Единственный источник правды о статусе. Обновляется **в том же коммите**, что и код задачи.
> Статусы: `todo` · `in_progress` · `done` · `blocked` · `cancelled`.
> Спецификация задач — [`PLAN.md`](PLAN.md). Протокол — [`CLAUDE.md`](../../CLAUDE.md).

## Текущая задача

<!-- Заполняется при взятии задачи, очищается при её завершении. Это чекпоинт для продолжения после обрыва сессии. -->

- **Задача:** —
- **Начата:** —
- **Шаги:**
  - [ ] —
- **Заметки для следующей сессии:** —

## Статус задач

| ID | Задача | Зависит от | Статус | Коммит | Примечание |
|---|---|---|---|---|---|
| P0.1 | Проект, инструменты, git | — | done |  |  |
| P0.2 | Перенос схемы и базовые типы | P0.1 | done |  |  |
| P0.3 | Тестовая инфраструктура | P0.2 | done |  |  |
| P0.4 | Миграции Alembic, exclude_identity_hub | P0.3 | todo | | |
| P1.1 | Ошибки и примитивы | P0.2 | todo | | |
| P1.2 | RegionMachine | P1.1 | todo | | |
| P1.3 | VerificationFlow / Spec / Tier | P1.1 | todo | | |
| P1.4 | Декларация Service и протоколы | P1.1 | todo | | |
| P1.5 | Вычисление допустимости операции | P1.4 | todo | | |
| P1.6 | Данные аккаунта, search_text, парсер поиска | P1.1 | todo | | |
| P1.7 | Криптография | P1.1 | todo | | |
| P1.8 | Каталог событий | P1.1 | todo | | |
| P2.1 | IdentityHub, SpaceHandle, транзакции | P0.4, P1.2, P1.4, P1.7, P1.8 | todo | | |
| P2.2 | Outbox событий | P2.1 | todo | | |
| P2.3 | Регионы в БД | P2.2 | todo | | |
| P2.4 | register | P2.3, P1.6 | todo | | |
| P2.5 | Изменение данных аккаунта, identity_data | P2.4 | todo | | |
| P2.6 | Связи и read-модели связей | P2.4 | todo | | |
| P3.1 | Pool и SQL выборки кандидатов | P1.5, P2.4 | todo | | |
| P3.2 | Захват аренды | P3.1 | todo | | |
| P3.3 | Жизненный цикл Lease (heartbeat, fencing) | P3.2 | todo | | |
| P3.4 | Fencing-записи и секреты под арендой, revoke | P3.3 | todo | | |
| P4.1 | Квоты, pacing, ключи операций | P3.3 | todo | | |
| P4.2 | lease.operation | P4.1 | todo | | |
| P5.1 | Прокси и их здоровье | P4.2 | todo | | |
| P5.2 | Привязка прокси | P5.1 | todo | | |
| P5.3 | Оркестрация входа, challenge | P3.4, P2.6 | todo | | |
| P5.4 | Верификации в БД | P1.3, P3.4 | todo | | |
| P5.5 | Пробы, resync, reconcile | P5.3, P5.4, P4.2 | todo | | |
| P5.6 | Секреты для админки | P3.4 | todo | | |
| P5.7 | import_many | P5.2, P5.4, P2.6 | todo | | |
| P6.1 | Поиск | P2.5, P3.3 | todo | | |
| P6.2 | Карточки и статистика | P6.1 | todo | | |
| P7.1 | Контекст логов | P3.3 | todo | | |
| P7.2 | Sink ih_lease_logs | P7.1 | todo | | |
| P8.1 | Диспетчер | P2.2 | todo | | |
| P8.2 | Агрегированные события | P8.1, P3.3 | todo | | |
| P8.3 | Исходящие вебхуки | P8.1 | todo | | |
| P8.4 | Telegram-уведомления | P8.3 | todo | | |
| P9.1 | Maintenance | P5.4, P3.3 | todo | | |
| P9.2 | CLI (migrate, dispatcher, maintenance, doctor, rotate-key) | P9.1, P8.1 | todo | | |
| P9.3 | Метрики | P4.2 | todo | | |
| P10.1 | MemoryStore | P2–P9 | todo | | |
| P10.2 | identity_hub.testing | P10.1 | todo | | |
| P10.3 | Нагрузочные тесты | P10.2 | todo | | |
| P10.4 | Документация и примеры | P10.3 | todo | | |
| P10.5 | Финальный аудит и трассировка | P10.4 | todo | | |

**Итого:** 3 / 47 done

## Журнал сессий

<!-- Добавлять строку в конец после каждой завершённой задачи или остановки. Формат:
YYYY-MM-DD · ID · результат (done / partial / blocked) · коротко что сделано · что дальше -->
2026-09-26 · P0.1 · done · uv-проект identity-hub, src-раскладка, ruff/mypy/pytest, утилита progress_tool.py · дальше P0.2
2026-09-26 · P0.2 · done · enums.py, _ids.py (без фолбэка uuid4), postgres/models.py, schema_options (D-013), pg-тесты схемы · дальше P0.3
2026-09-26 · P0.3 · done · фикстуры database_url/engine/prepared_db/db (TRUNCATE), docker-compose · дальше P0.4
