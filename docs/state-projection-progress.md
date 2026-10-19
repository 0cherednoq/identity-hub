# Прогресс state projection

> Единственный источник правды о статусе очереди `SP*`.
> Обновляется в том же коммите, что код или документация задачи.
> Статусы: `todo`, `in_progress`, `done`, `blocked`, `cancelled`.
> Спецификация: [`state-projection-plan.md`](state-projection-plan.md).
> Источник требований: [`isue/ISSUE-identity-hub-state-projection.md`](isue/ISSUE-identity-hub-state-projection.md).
> Решение о границах: `D-325` в [`plan/DECISIONS.md`](plan/DECISIONS.md).

## Текущая задача

<!-- Заполняется при взятии задачи и очищается при завершении. Это checkpoint после обрыва сессии. -->

Нет активной задачи: очередь `SP*` завершена.

## Статус задач

| ID | Задача | Зависит от | Статус | Коммит | Примечание |
|---|---|---|---|---|---|
| SP0.1 | Baseline и воспроизведение дефектов `SP-1`, `SP-2` | — | done |  | baseline `skip` и оба дефекта закреплены conformance-тестами |
| SP1.1 | Политика `failure_policy="block"` | SP0.1 | done |  | одиночная/пакетная доставка, валидация, затухание логов и метрика попыток |
| SP1.2 | Видимость застрявшего потребителя | SP1.1 | done |  | `stalled` и `failure_attempts` доступны в runtime и background snapshot |
| SP2.1 | Не удалять журнал ниже курсоров потребителей | SP0.1 | done |  | Memory/PostgreSQL; отчёт и метрика называют держателя |
| SP2.2 | Явная ошибка истёкшего курсора | SP2.1 | done |  | публичная ошибка, doctor horizon warning, документированный rebuild |
| SP3.1 | `verification_tier_rank` в read-схему | — | done |  | миграция `0038`, read-schema v5, пересчёт факторов/конфигурации |
| SP3.2 | Ревизия в результате импорта | — | done |  | совместимые revision maps в отчёте и трёхаргументном on_batch |
| SP4.1 | Рецепт внешней read-модели и финальная приёмка | SP1.2, SP2.2, SP3.1, SP3.2 | done |  | рецепт, три интеграционных теста, индексы и полный gate |

**Итого:** 8 / 8 done

## Журнал сессий

2026-10-19 · план · ишью `ISSUE-identity-hub-state-projection.md` переписан после сверки с кодом:
три посылки опровергнуты, подтверждены пробелы `SP-1`…`SP-5`; создана очередь `SP0`–`SP4`,
зафиксировано решение `D-325`; реализация не начата · дальше `SP0.1`

2026-10-19 · реализация · закрыты `SP0.1`, `SP1.1`, `SP1.2`, `SP2.1`: добавлена политика
`failure_policy="block"` для одиночных и пакетных подписчиков, валидация конфликта с
`max_attempts`, затухающие предупреждения и метрика попыток; runtime/background snapshot явно
показывают `stalled` и число попыток; retention ограничен минимальным курсором на Memory и
PostgreSQL, отчёт и метрика называют удерживающего потребителя. Проверки: targeted dispatcher
`22 passed`; retention/maintenance `14 passed`; `mypy src tests/typing` и targeted `ruff`
зелёные · дальше `SP2.2`.

2026-10-19 · реализация · закрыт `SP2.2`: retention сохраняет событие-якорь минимального
курсора; `ConsumerCursorExpired` экспортирована публично и не поглощается dispatcher; doctor
всегда видит истёкший курсор и предупреждает о курсоре на горизонте после 90% retention;
документирован rebuild через очистку read-модели и удаление строки `ih_event_cursors`.
Проверки: профильный pytest `63 passed`, `mypy` и `ruff` зелёные · дальше `SP3.1`.

2026-10-19 · реализация · закрыт `SP3.1`: миграция `0038` добавляет nullable
`verification_tier_rank`; rank меняется атомарно с `verified_factors` и пересчитывается для
существующих identity при синхронизации изменённой конфигурации `Service.tiers`; публичная
read-схема v5 отдаёт колонку напрямую. Проверки: migrations/read-schema/KYC `18 passed`,
unit `42 passed`, mypy зелёный · дальше `SP3.2`.

2026-10-19 · реализация · закрыт `SP3.2`: `ImportReport` сохранил списки `created`/`updated`
и добавил карты `created_revisions`/`updated_revisions`; новый трёхаргументный `on_batch`
получает карту revision для created и updated внутри транзакции, двухаргументный контракт не
изменён. Проверки: imports/public API `28 passed`, mypy/ruff зелёные · дальше `SP4.1`.

2026-10-19 · реализация · закрыт `SP4.1` и вся очередь: руководство описывает порядок
«подписка → snapshot → поток», revision-guard, удаление, restart, retention, rebuild и сверку;
три PostgreSQL-интеграционных сценария проверяют гонку snapshot, durable restart и повтор пачки.
Миграция `0038` добавляет индексы `(space_id, id)` для snapshot и `(last_tx_id, last_id)` для
retention. Финальный аудит подтвердил все десять критериев issue. Проверки: conformance-shards
`653 passed` и `369 passed, 1 skipped`; PostgreSQL/unit/public API `1211 passed, 4 skipped`;
ruff, mypy и import-linter зелёные.
