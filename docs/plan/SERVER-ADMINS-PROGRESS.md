# Прогресс исправлений по ишью server-admins

> Единственный источник правды о статусе очереди `S` (ишью 01–12 из `docs/isue/server-admins/`, первичный аудит 01–08 в `docs/isue/09-assessment.md`; запросы владельца `D-311`–`D-314`).
> Обновляется в том же коммите, что и код задачи.
> Статусы: `todo` · `in_progress` · `done` · `blocked` · `cancelled`.
> Спецификация: [`SERVER-ADMINS-PLAN.md`](SERVER-ADMINS-PLAN.md).
> Требования: [`../isue/09-assessment.md`](../isue/09-assessment.md). Протокол: [`CLAUDE.md`](../../CLAUDE.md).

## Текущая задача

<!-- Заполняется при взятии задачи и очищается при завершении. Это checkpoint для продолжения после обрыва. -->

- **Задача:** S20.2 Финальная сверка и выпуск исправления
- **Начата:** 2026-10-16
- **Шаги:**
  - [x] сводные targeted и typing проверки
  - [x] PostgreSQL gate после D-323 migration 0037
  - [x] полный scripts/check.sh (эквивалентный split-run)
  - [ ] версия, changelog и release
  - [ ] публичная установка из PyPI
- **Заметки для следующей сессии:** S13.1/S13.2 и S17-S20 реализованы локально; полный split-gate — 2191 passed, 5 skipped, Ruff/mypy/import-lint green. Остались версия/release и проверка PyPI.

## Статус задач

| ID | Задача | Зависит от | Статус | Коммит | Примечание |
|---|---|---|---|---|---|
| S0.1 | Измерение `code` у `logins.failed` | — | done |  | ишью 02, P0 |
| S0.2 | `deleted` терминален для наблюдений | — | done |  | ишью 04, P0 |
| S1.1 | `LoginFailure`: код отказа в карточке, событии и операции | S0.1 | done |  | ишью 01, P1 |
| S1.2 | `LoginFailure.fault`: репутация при входе одной транзакцией | S0.2, S1.1 | done |  | ишью 07, P1 |
| S2.1 | `Fault.until` | S1.2 | done |  | ишью 03, P2 |
| S2.2 | Временный бан | S2.1 | done |  | ишью 03; запрос владельца, `D-311` |
| S3.1 | Документировать замену credentials и `cool_down` из карантина | — | done |  | ишью 05, 06 |
| S3.2 | Явный сброс кода входа при замене credentials | S1.1, S3.1 | done |  | ишью 05, P2 |
| S4.1 | Финальная сверка | S0.1, S0.2, S1.1, S1.2, S2.1, S2.2, S3.1, S3.2 | done |  | закрытие |
| S5.1 | Режим `secret_storage="plaintext"`: схема, запись, чтение | — | done |  | запрос владельца, `D-313`; миграция |
| S5.2 | Перевод хранения, предупреждения и документация | S5.1 | done |  | запрос владельца, `D-313` |
| S6.1 | Модели, нормализация и каркас `dump`/`load` | — | done |  | запрос владельца, `D-314` |
| S6.2 | Браузеры: `dump`/`load` и методы аренды | S6.1 | done |  | `D-314` |
| S6.3 | HTTP-клиенты: `dump`/`load` | S6.1 | done |  | `D-314` |
| S6.4 | Файлы и автоопределение | S6.1, S6.2 | done |  | `D-314` |
| S6.5 | Отпечаток: `dump`/`load` | S6.1, S6.2 | done |  | `D-314` |
| S6.6 | Интеграция с хабом и документация | S5.2, S6.1, S6.2, S6.3, S6.4, S6.5 | done |  | `D-314`; закрытие S5–S6 |
| S7.1 | `AcceptPolicy`: модель, дефолт сервиса, постановка на приёмку | — | done |  | запрос владельца, `D-317` |
| S7.2 | `AcceptanceWorker`: применение политики, retryable, закрытие фазы | S7.1 | done |  | `D-317` |
| S8.1 | `imports.run` пишет `identity.registered` на аккаунт | — | done |  | запрос владельца, `D-318` |
| S9.1 | Отпечаток значения и признак валидности у записи | — | done |  | запрос владельца, `D-319`; миграция |
| S9.2 | Пометка валидности из адаптера и учёт в приёмке | S9.1 | done |  | `D-319` |
| S9.3 | Импорт с накоплением: режим `OnExisting.MERGE` | S9.1 | done |  | `D-319` |
| S9.4 | Документация и закрытие фазы | S9.1, S9.2, S9.3 | done |  | `D-319` |
| S10.1 | Связи аккаунтов в read-схеме | — | done |  | запрос владельца, `D-320` |
| S10.2 | Предикаты связей в `IdentityFilter` и аренде | S10.1 | done |  | `D-320` |
| S11.1 | Колонка `url` у сервиса | — | done |  | запрос владельца, `D-321`; миграция |
| S11.2 | Доступ к кредам: `active_credential` и типизация | — | done |  | `D-321` |
| S12.1 | Формат `eazy` в конвертерах cookies и сессии | — | done |  | запрос владельца, `D-322` |
| S12.2 | `lease.session_revision` и модуль `integrations.eazy` | S12.1 | done |  | `D-322` |
| S13.1 | Plaintext для всех секретных хранилищ | — | done |  | `D-323`; миграция `0037` |
| S13.2 | Plaintext по умолчанию | S13.1 | done |  | `D-323` |
| S17.1 | Ключ и значение из `active_credential` | — | done |  | issue 09, P0 |
| S17.2 | Временный обход и миграция документации | S17.1 | done |  | issue 09 |
| S18.1 | `retryable` в карточке identity | — | done |  | issue 10, P1 |
| S19.1 | `put_credential(reset_auth=...)` | — | done |  | issue 11, P1 |
| S20.1 | Ревизия схемы в changelog и release-gate | — | done |  | issue 12, P1 |
| S20.2 | Финальная сверка и выпуск исправления | S17.1, S17.2, S18.1, S19.1, S20.1 | in_progress |  | закрытие issues 09–12 |

**Итого:** 37 / 38 done

## Журнал сессий
2026-10-15 · S0.1 · done · code во всех login.failed и legacy fallback rollup
2026-10-15 · S0.2 · done · deleted остаётся терминальным вне явного override
2026-10-15 · S1.1 · done · типизированный LoginFailure и безопасный код в карточке, событии и операции
2026-10-15 · S1.2 · done · общая атомарная политика Fault для login и provider operations
2026-10-15 · S2.1 · done · Fault.until валидируется и передаётся только для поддерживаемого standing
2026-10-15 · S2.2 · done · временный banned снимается по until, бессрочный остаётся
2026-10-15 · S3.1 · done · прежняя замена credentials сохраняет auth и session; карантин не ослабляется
2026-10-15 · S3.2 · done · replace_credentials(reset_auth=True) атомарно очищает прошлый отказ входа
2026-10-15 · S4.1 · done · финальная документация и traceability; 2047 passed; anti-flake 3×330 passed
2026-10-15 · S5.1 · done · dual-format encrypted/plaintext storage, migration 0033, mixed reads and tests
2026-10-15 · S5.2 · done · batch conversion, CLI, warnings, audit and 1000-row round trip
2026-10-15 · S6.1 · done · canonical models, RFC OTP, JSON namespaces and per-slot session schemas
2026-10-15 · S6.2 · done · Playwright storage_state, CDP/Puppeteer, Selenium, extension/Crawlee/tough-cookie и lease helpers; 26 targeted tests
2026-10-15 · S6.3 · done · CookieJar/aiohttp/Set-Cookie/Scrapy, строгая область cookie, ProxyUrl; D-315; 40 passed, 2 optional skipped
2026-10-15 · S6.4 · done · Netscape/HAR, safe autodetect, base64, explicit line templates and unsafe binary rejection; 51 passed, 2 optional skipped
2026-10-15 · S6.5 · done · Browserforge/fingerprint-suite, browser and antidetect dumps, versioned impersonation targets; 93 passed, 2 optional skipped
2026-10-16 · S6.6 · done · Hub integration, nested fingerprint filters, guide/traceability; full check 2128 passed, 5 skipped; anti-flake 3x60 passed
2026-10-16 · S7.1 · done · AcceptPolicy, default_accept, migration 0034 and bulk durable queue; 6 tests on MemoryStore/PostgreSQL
2026-10-16 · S7.2 · done · AcceptanceWorker, auth retryable and import checker docs; full check 2136 passed, 5 skipped; anti-flake 3x68 passed
2026-10-16 · S8.1 · done · identity.registered на каждый созданный импортом аккаунт; 41 целевой тест, полный gate 2157 passed
2026-10-16 · S9.1 · done · migration 0035, canonical HMAC and validity metadata on both backends
2026-10-16 · S9.2 · done · adapter marks exact credential; acceptance retries while a fresh candidate remains
2026-10-16 · S9.3 · done · merge accumulation, deterministic names, active-session keep/replace and four-case matrix
2026-10-16 · S9.4 · done · docs and public contract; full gate 2157 passed, 5 skipped; anti-flake 3x74 passed
2026-10-16 · S10.1 · done · read-схема identity_links и PostgreSQL anti-join
2026-10-16 · S10.2 · done · link-aware find/list/count и эксклюзивная аренда · очередь S закрыта
2026-10-16 · S11.1 · done · Service.url origin, migration 0036, catalog and read schema
2026-10-16 · S11.2 · done · active_credential, generic schema typing и schema= validation · дальше S12.1
2026-10-16 · S12.1 · done · eazy cookie primitives и lazy CookieState bridge · дальше S12.2
2026-10-16 · S12.2 · done · session_revision и fenced HubSessionStore; split full gate 2173 passed, 5 skipped · очередь закрыта
2026-10-16 · S17.1 · done · CredentialCandidate(kind, name, value), точная пометка отказа; Ruff, mypy 165 files, 29 targeted passed · дальше S17.2
2026-10-16 · S17.2 · done · обход ограничен версиями <=1.0.0a3; guides и исторический план используют CredentialCandidate; 13 docs/public tests passed · дальше S18.1
2026-10-16 · S18.1 · done · RegionInfo.retryable, admin API/UI tri-state; Memory 16 passed, admin 1 passed; PostgreSQL gate отложен из-за незавершённой D-323 schema 0037 · дальше S19.1
2026-10-16 · S19.1 · done · put_credential(reset_auth=True), общий транзакционный helper, rollback/session/authenticated тесты; Memory 16 passed, Ruff/mypy green; PostgreSQL gate в S20.2 · дальше S20.1
2026-10-16 · S20.1 · done · CHANGELOG и GitHub Release исправлены на 0036; pre-build release metadata gate, 6 tests passed · дальше S20.2
2026-10-16 · S13.1 · done · migration 0037, dual-format всех девяти наборов, doctor round-trip и защищённый downgrade; 39 targeted passed · дальше S13.2
2026-10-16 · S13.2 · done · plaintext стал дефолтом, encrypted fixtures явные, guides/CLI/warning обновлены · дальше S20.2 full gate
2026-10-16 · S20.2 · in_progress · полный эквивалент scripts/check.sh: 2191 passed, 5 skipped; Ruff, mypy 166 files и import-lint green · дальше version/release/PyPI
