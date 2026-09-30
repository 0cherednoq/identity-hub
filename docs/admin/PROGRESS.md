# Прогресс Identity Hub Admin

> Единственный источник правды о статусе задач `A*`. Обновляется в том же коммите, что код задачи.
> Допустимые статусы: `todo`, `in_progress`, `done`, `blocked`, `cancelled`.
> Спецификация: [`PLAN.md`](PLAN.md). Протокол: [`AUTONOMY.md`](AUTONOMY.md).

## Текущая задача

<!-- Этот блок изменяет progress_tool.py. Одновременно активна не более чем одна задача. -->

- **Задача:** A4.6 Страница аналитики
- **Начата:** 2026-09-27
- **Шаги:**
  - [x] зафиксировать URL state и карту аналитических панелей
  - [x] собрать API queries и typed view-models
  - [x] реализовать accessible charts tables и coverage
  - [x] добавить export data image и empty/error states
  - [x] покрыть unit E2E visual длинные legends
  - [ ] обновить документацию и пройти ворота
- **Заметки для следующей сессии:** —

## Внешние ворота

Статусы `P*` не копируются в этот файл. `progress_tool.py next` читает их из
[`docs/plan/PROGRESS.md`](../plan/PROGRESS.md). Если библиотечная задача ещё не `done`, зависимая
задача `A*` не считается доступной.

## Статус задач

| ID | Задача | Зависит от | Статус | Коммит | Примечание |
|---|---|---|---|---|---|
| A0.1 | Аудит исходных контрактов | — | done |  |  |
| A0.2 | Каркас отдельного пакета в workspace | A0.1 | done |  |  |
| A0.3 | Тестовая инфраструктура и CI-каркас | A0.2, P0.4 | done |  |  |
| A0.4 | Формальный контракт API | A0.2 | done |  |  |
| A0.5 | Локальная среда и демонстрационные данные | A0.3, A1.1 | done |  |  |
| A1.1 | Litestar app, настройки, CLI и раздача SPA | A0.2 | done |  |  |
| A1.2 | Аутентификация, сессии, CSRF и RBAC | A0.4, A1.1 | done |  |  |
| A1.3 | Ограничение пространства, actor и идемпотентность | A1.2 | done |  |  |
| A1.4 | OpenAPI и сгенерированный TypeScript-клиент | A0.4, A1.1 | done |  |  |
| A1.5 | Дизайн-система и каталог /_design | A0.2 | done |  |  |
| A1.6 | App shell, router, query cache и URL state | A1.2, A1.4, A1.5 | done |  |  |
| A1.7 | Наблюдаемость и обработка ошибок | A1.1, A1.6, P7.1 | done |  |  |
| A1.8 | Базовый E2E, accessibility и visual harness | A0.3, A1.6 | done |  |  |
| A2.1 | Обзор, пространства и страница пространства | A1.8, P6.2 | done |  |  |
| A2.2 | Поиск и таблица аккаунтов | A2.1, P6.1 | done |  |  |
| A2.3 | Карточка аккаунта: режим чтения | A2.2, P6.2 | done |  | Безопасная карточка v2, revision/heartbeat/namespace/quota/verification metadata, 8 lazy deep-linked вкладок, явные 404 и secret-free responses готовы; 435 library, 57 backend, 19 unit и 19 E2E проверок проходят · дальше A2.4 |
| A2.4 | Страница аренд | A1.8, P6.2 | done |  | Оконные lease read-модели, exact overlap-utilization, fixture p95, group by pool/service/holder, live heartbeat/expired-unclosed, cursor history, график и polling с URL-фильтрами готовы; 435 library (+4 expected xfail), 58 backend, 19 unit и 20 E2E проверок проходят · дальше A2.5 |
| A2.5 | Очередь нужен человек: режим чтения | A1.8, P5.4, P5.5 | done |  |  |
| A2.6 | Страница прокси | A1.8, P5.2 | done |  |  |
| A2.7 | Журнал событий | A1.8, P8.1 | done |  |  |
| A2.8 | Справочник сервисов | A1.8, P1.4 | done |  |  |
| A2.9 | Состояние фоновых работ | A1.8, P8.3, P8.4, P9.1 | done |  |  |
| A2.10 | Уведомления и вебхуки: режим чтения | A1.8, P8.4 | done |  |  |
| A3.1 | Единый command gateway | A1.3, A2.3 | done |  |  |
| A3.2 | Lifecycle, state override и requirements | A3.1, P5.5 | done | --notes |  |
| A3.3 | Login, challenge и verification actions | A3.1, A2.5, P5.5 | done |  |  |
| A3.4 | Аренды, прокси и связи | A3.1, A2.4, A2.6, P5.2 | done |  |  |
| A3.5 | Показ и обновление секретов | A3.1, P5.6 | done |  |  |
| A3.6 | Массовые действия и экспорт | A3.2, A2.2, A2.7 | done |  |  |
| A3.7 | Импорт и отчёты запусков | A3.1, A4.1, P5.7 | done |  |  |
| A3.8 | Управление уведомлениями и вебхуками | A3.1, A2.10, P8.4 | done |  |  |
| A4.1 | Миграции данных админки | A0.1, P9.1 | done |  |  |
| A4.2 | События tiers и MetricsHook | A4.1, P9.3 | done |  |  |
| A4.3 | Идемпотентные временные роллапы | A4.2, P8.1 | done |  |  |
| A4.4 | Почасовые снимки состояния | A4.1, P9.1 | done |  |  |
| A4.5 | Read-модели аналитики | A4.3, A4.4 | done |  |  |
| A4.6 | Страница аналитики | A4.5, A1.5 | in_progress |  |  |
| A4.7 | Тревоги и исторические блоки обзора | A4.5, A2.1 | todo | | |
| A5.1 | UX, accessibility и адаптивность | A3.2, A3.3, A3.4, A3.5, A3.6, A3.7, A3.8, A4.6, A4.7 | todo | | |
| A5.2 | Производительность и устойчивость | A5.1, P10.3 | todo | | |
| A5.3 | Аудит безопасности | A5.2 | todo | | |
| A5.4 | Wheel, CLI и Litestar plugin | A5.3 | todo | | |
| A5.5 | Docker и deployment profiles | A5.4 | todo | | |
| A5.6 | Release CI и supply chain | A5.5 | todo | | |
| A5.7 | Эксплуатационная документация | A5.5 | todo | | |
| A5.8 | Финальный аудит и release candidate | A5.6, A5.7, P10.5 | todo | | |

**Итого:** 36 / 46 done

## Журнал сессий

<!-- Формат одной строки:
YYYY-MM-DD · ID · done|partial|blocked · что сделано · что дальше
-->
2026-09-27 · A0.1 · done · Контрактная матрица 14 страниц создана, расхождения исправлены, решения A-D100–A-D103 и D-122 зафиксированы
2026-09-27 · A0.2 · done · Создан отдельный Python/Litestar и Vite/React workspace, lock-файлы и единые команды; clean checks и wheel build проходят
2026-09-27 · A0.3 · done · PostgreSQL 16 fixtures, space isolation, роли, fake transports, request ID, Chromium trace harness и четыре CI jobs проходят
2026-09-27 · A0.4 · done · Строгие DTO/envelopes, cursor/idempotency/time-series, error registry, route metadata и deterministic OpenAPI с 13 contract-тестами готовы
2026-09-27 · A1.1 · done · Реализованы типизированные настройки, lifecycle/readiness, безопасная SPA-раздача, CLI и 29 backend-тестов; frontend build, OpenAPI и Playwright smoke зелёные.
2026-09-27 · A0.5 · done · Безопасный in-memory demo с 4 сценариями и 258 аккаунтами; seed идемпотентен, production запрещён; единый dev supervisor проверен на запуск и shutdown; PowerShell/Unix инструкции добавлены.
2026-09-27 · A1.2 · done · Bearer/password/trusted-proxy auth, signed Secure session, CSRF, exact RBAC, login/reveal limits and security headers implemented; 46 backend tests, contract/OpenAPI, frontend checks, wheel and Chromium E2E pass.
2026-09-27 · A1.3 · done · Центральный space guard + non-creating IdentityHub.get_space, server-derived actor/request context, scoped object lookup and concurrent idempotency replay/conflict/retry implemented; 50 admin + 5 library tests pass.
2026-09-27 · A1.4 · done · Pinned Hey API 0.99.0 with audited js-yaml override, deterministic 16-file client, strict fetch transport, exhaustive error unions and hash-based CI drift check; audit/build/E2E pass.
2026-09-27 · A1.5 · done · Токены и темы, CLI-generated shadcn primitives, композиты, 11 status registries, lint architecture fixtures и стабильные light/dark snapshots готовы · дальше A1.6
2026-09-27 · A1.6 · done · Typed TanStack Router, auth shell, safe preferences, error boundaries и query cache lifecycle готовы; 16 unit и 4 Chromium E2E проходят · дальше доступная задача
2026-09-27 · A1.7 · done · Structured access logs, diagnostics/metrics, frontend error reporting, graceful timeouts и сквозной request ID проверены · дальше A1.8
2026-09-27 · A1.8 · done · Три role helpers, реальный login/session/logout, viewer 403, axe/keyboard smoke и 4 shell visual baselines готовы; 54 backend, 18 unit и 13 E2E тестов проходят · дальше A2.1
2026-09-27 · A2.1 · done · Безопасные overview read-модели, versioned API, global/spaces/space UI, service slices и URL drill-down готовы; SQL oracle, 432 library, 55 backend, 18 unit и 13 E2E проверок проходят · дальше A2.2
2026-09-27 · A2.2 · done · Безопасная account read-модель, все фильтры §3.4, server-side stable keyset sorting, URL-presets, настраиваемые колонки и selection готовы; 434 library, 56 backend, 19 unit и 17 E2E проверок проходят · дальше A2.3
2026-09-27 · A2.5 · done · Единая secret-free human queue для challenge/requirements/manual review/drift/quarantine, urgency/deadline keyset, scoped API, URL-фильтры, polling, disabled actions и account deep links готовы; 437 library (+4 expected xfail), 59 backend, 21 unit и 21 E2E проходят · дальше A2.6
2026-09-27 · A2.6 · done · Secret-free proxy overview/detail, exact binding capacity, health, history, URL-фильтры, polling и deep links готовы; 440 core (+3 expected xfail), 60 backend, 24 frontend unit и 23 E2E проходят · дальше A2.7
2026-09-27 · A2.7 · done · Каталожный scoped event journal, все фильтры, устойчивый keyset, safe summary, redacted drawer и deep links готовы; 441 core (+4 expected xfail), 61 backend, 26 frontend unit и 24 E2E проходят · дальше A2.8
2026-09-27 · A2.8 · done · Детерминированный secret-free Service catalog, scoped counts, операции/квоты, схемы, verification tiers/codes, proxy policy и explicit unsupported UI готовы; 442 core (+4 expected xfail), 62 backend, 26 frontend unit и 25 E2E проходят · дальше A2.9
2026-09-27 · A2.9 · done · Scoped background diagnostics, runtime heartbeat, cursor lag, delivery queues, maintenance/stale probes, backend health thresholds и visible-tab 10s polling готовы; 443 core (+4 expected xfail), 63 backend, 26 frontend unit и 26 E2E проходят · дальше A2.10
2026-09-27 · A2.10 · done · Secret-free scoped delivery center: Telegram channels/rules, webhook endpoints, dual cursor journals, normalized errors, masked targets and immutable 24h previews; 444 core (+4 expected xfail), 64 backend and 27 E2E checks pass.
2026-09-27 · A3.1 · done · Typed atomic command gateway, confirm/dry-run DTO, row-locked revision guard, safe error mapping, shared frontend command protocol and conflict dialog complete; 445 core (+3 expected xfail), 66 backend, 28 frontend unit and 27 E2E checks pass.
2026-09-27 · A3.2 · done · Server-derived lifecycle/override/unpin/restore/requirement actions, role filtering, atomic revision checks and isolated account/queue UI complete; 446 core (+4 expected xfail), 69 backend, 29 frontend unit and 28 deterministic E2E checks pass.
2026-09-27 · A3.3 · done · Selected login/resync, encrypted challenge answer, expiry guard, manual verification decisions and queue quick flow complete; 70 backend, 30 frontend unit and 28 E2E checks pass.
2026-09-27 · A3.4 · done · Revoke lease с fencing, атомарный capacity-aware proxy rebind, recheck/override, link/unlink с impact-confirmation и scoped UI готовы; 269 targeted core, 71 backend, 32 frontend unit и 28 E2E проверок проходят
2026-09-27 · A3.5 · done · Admin-only reveal/update с повторной аутентификацией, no-store, audit, rate-limit, fencing session и ephemeral frontend state готовы; 447 library, 73 backend, 34 frontend unit, 29 E2E и 6 visual проверок проходят · дальше следующая доступная задача
2026-09-27 · A3.6 · done · Server-side snapshot по фильтру, bounded bulk lifecycle/resync/tag с partial retry, CSV/JSON accounts/events export с formula sanitation и аудитом готовы; 447 library, 81 backend, 35 frontend unit и 30 E2E проверок проходят · дальше следующая доступная задача
2026-09-27 · A3.8 · done · CRUD channels/rules/webhooks, one-time rotation, isolated test-send, audited idempotent retry и retry policy UI готовы; 451 library, 84 backend, 37 frontend unit, 31 E2E и 6 visual проверок проходят
2026-09-27 · A4.1 · done · Ревизия 0005 добавляет timezone, durable import/provider state и индексы §5; upgrade/downgrade, schema drift, retention и 456 library tests зелёные · дальше A3.7
2026-09-27 · A3.7 · done · Возобновляемый зашифрованный CSV/JSONL импорт, cancel boundary и redacted report; 462 library + 87 backend + 40 frontend + PG/E2E зелёные · дальше по next
2026-09-27 · A4.2 · done · tier events и bounded MetricsHook готовы; 465 passed, 4 known xfail · дальше A4.3
2026-09-27 · A4.3 · done · 1m/1h/1d event и telemetry rollups, rebuild, retention и dense series готовы; 475 passed, 3 known xfail, admin 87 passed · дальше A4.4
2026-09-27 · A4.4 · done · hourly state snapshots, explicit coverage, safe backfill и retention готовы; 481 passed, 4 known xfail · дальше A4.5
2026-09-27 · A4.5 · done · analytics catalog, bounded read-models, formulas, DST/coverage, ETag API и generated client готовы; 484 library, 89 backend и 40 frontend unit проверок проходят · дальше A4.6
