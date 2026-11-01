# Прогресс очереди перепроверки аккаунта (auth revalidation)

> Единственный источник правды о статусе очереди `AR` (решение `D-335`).
> Обновляется в том же коммите, что и код задачи.
> Статусы: `todo` · `in_progress` · `done` · `blocked` · `cancelled`.
> Спецификация: [`AUTH-REVALIDATION-PLAN.md`](AUTH-REVALIDATION-PLAN.md).
> Требования и обоснование: [`../isue/auth-revalidation-review.md`](../isue/auth-revalidation-review.md)
> (эксперименты E1–E6, ресерч, решение), исходное RFC —
> [`../isue/auth-revalidation-architecture.md`](../isue/auth-revalidation-architecture.md).
> Протокол: [`CLAUDE.md`](../../CLAUDE.md).

## Текущая задача

<!-- Заполняется при взятии задачи и очищается при завершении. Это checkpoint для продолжения после обрыва. -->

- **Задача:** AR1.1 Сетка projection_failed: операция не остаётся running
- **Начата:** 2026-11-01
- **Шаги:**
  - [x] Изучить D-220 и пути проекции (apply допускает финализацию отдельной tx по тому же claim)
  - [x] Красные тесты: verify/adopt, проба, login, challenge
  - [ ] Helper _projection_guard и применение в четырёх путях
  - [ ] Документация и CHANGELOG
  - [ ] scripts/check.sh зелёный, закрыть задачу
- **Заметки для следующей сессии:** D-220 допускает: после отката проекции операция running с тем же claim, apply_provider_operation(failed) отдельной tx; тесты test_projection_failed.py красные; дальше helper в sync.py (guard = два контекста в одном async with)

## Статус задач

| ID | Задача | Зависит от | Статус | Коммит | Примечание |
|---|---|---|---|---|---|
| AR0.1 | Вердикт провайдера как `Observation`, защита `challenge` | — | done |  | L0; дефект, E1/E3/E6 |
| AR1.1 | Сетка `projection_failed`: операция не остаётся `running` | AR0.1 | in_progress |  | L1 |
| AR1.2 | Recovery: `outcome_unknown` и метрика | AR1.1 | todo |  | L1; E5 |
| AR2.1 | `DiagnosticPolicy` и типизированные `Blocker` | AR0.1 | todo |  | L2, модель без поведения |
| AR2.2 | Диагностический допуск: выбор кандидата и гейт внутри lease | AR2.1 | todo |  | L2; E2 |
| AR3.1 | `auth.revalidate()`: каркас, шаг сессии, структурный результат | AR1.1, AR2.2 | todo |  | L3 |
| AR3.2 | `auth.revalidate()`: шаг credential без `logging_in` | AR3.1 | todo |  | L3 |
| AR3.3 | `request_id`: идемпотентность и конкурентность | AR1.2, AR3.2 | todo |  | L3 |
| AR4.1 | Документация, traceability и финальная сверка | AR0.1, AR1.1, AR1.2, AR2.1, AR2.2, AR3.1, AR3.2, AR3.3 | todo |  | закрытие |

**Итого:** 1 / 9 done

## Журнал сессий
2026-11-01 · AR0.1 · done · вердикт провайдера как Observation, sticky для challenge/logging_in, Observation.retryable; check.sh 2401 passed · дальше AR1.1
