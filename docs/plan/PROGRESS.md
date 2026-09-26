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
| P0.4 | Миграции Alembic, exclude_identity_hub | P0.3 | done |  |  |
| P1.1 | Ошибки и примитивы | P0.2 | done |  |  |
| P1.2 | RegionMachine | P1.1 | done |  |  |
| P1.3 | VerificationFlow / Spec / Tier | P1.1 | done |  |  |
| P1.4 | Декларация Service и протоколы | P1.1 | done |  |  |
| P1.5 | Вычисление допустимости операции | P1.4 | done |  |  |
| P1.6 | Данные аккаунта, search_text, парсер поиска | P1.1 | done |  |  |
| P1.7 | Криптография | P1.1 | done |  |  |
| P1.8 | Каталог событий | P1.1 | done |  |  |
| P2.1 | IdentityHub, SpaceHandle, транзакции | P0.4, P1.2, P1.4, P1.7, P1.8 | done |  |  |
| P2.2 | Outbox событий | P2.1 | done |  |  |
| P2.3 | Регионы в БД | P2.2 | done |  |  |
| P2.4 | register | P2.3, P1.6 | done |  |  |
| P2.5 | Изменение данных аккаунта, identity_data | P2.4 | done |  |  |
| P2.6 | Связи и read-модели связей | P2.4 | done |  |  |
| P3.1 | Pool и SQL выборки кандидатов | P1.5, P2.4 | done |  |  |
| P3.2 | Захват аренды | P3.1 | done |  |  |
| P3.3 | Жизненный цикл Lease (heartbeat, fencing) | P3.2 | done |  |  |
| P3.4 | Fencing-записи и секреты под арендой, revoke | P3.3 | done |  |  |
| P4.1 | Квоты, pacing, ключи операций | P3.3 | done |  |  |
| P4.2 | lease.operation | P4.1 | done |  |  |
| P5.1 | Прокси и их здоровье | P4.2 | done |  |  |
| P5.2 | Привязка прокси | P5.1 | done |  |  |
| P5.3 | Оркестрация входа, challenge | P3.4, P2.6 | done |  |  |
| P5.4 | Верификации в БД | P1.3, P3.4 | done |  |  |
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

**Итого:** 28 / 47 done

## Журнал сессий

<!-- Добавлять строку в конец после каждой завершённой задачи или остановки. Формат:
YYYY-MM-DD · ID · результат (done / partial / blocked) · коротко что сделано · что дальше -->
2026-09-26 · P0.1 · done · uv-проект identity-hub, src-раскладка, ruff/mypy/pytest, утилита progress_tool.py · дальше P0.2
2026-09-26 · P0.2 · done · enums.py, _ids.py (без фолбэка uuid4), postgres/models.py, schema_options (D-013), pg-тесты схемы · дальше P0.3
2026-09-26 · P0.3 · done · фикстуры database_url/engine/prepared_db/db (TRUNCATE), docker-compose · дальше P0.4
2026-09-26 · P0.4 · done · ветка миграций (ih_alembic_version), 0001 с pg_trgm, migrate(engine, db_schema), exclude_identity_hub, тест отсутствия дрейфа · дальше P1.1
2026-09-26 · P1.1 · done · иерархия исключений (picklable), Blocker, Clock/SystemClock/FakeClock · дальше P1.2
2026-09-26 · P1.2 · done · RegionSpec/apply: команды, наблюдения (stale, unknown, pin/drift, confirm_leaving, sticky), override, таймер; спецификации 6 регионов; D-102..D-104 · дальше P1.3
2026-09-26 · P1.3 · done · VerificationSpec/Tier, регион VERIFICATION (нормализация verified→missing⇒revoked), VerificationFlow, хелперы факторов · дальше P1.4
2026-09-26 · P1.4 · done · Service/Quota/Pacing/OperationRule/ProxyPolicy, протоколы расширения, ImportRecord, FAKE_TG/FAKE_EXCHANGE · дальше P1.5
2026-09-26 · P1.5 · done · evaluate() → blockers (lifecycle/standing/auth/availability lazy/verification+tiers/quota), compile_rule → RuleFilter · дальше P1.6
2026-09-26 · P1.6 · done · validate_labels/tags/model, split_profile, build_search_text, parse_query (D-105, D-106) · дальше P1.7
2026-09-26 · P1.7 · done · Envelope AES-256-GCM (DEK на запись, AAD, rewrap, sibling-поля), seal_model/open_model, Env/File/Static провайдеры · дальше P1.8
2026-09-26 · P1.8 · done · каталог FR-13.3 (30 внешних + журнал), EventDraft/Event, маски, severity_at_least, scrub (S-8, строгий режим) · фаза 1 закрыта, дальше P2.1
2026-09-26 · P2.1 · done · Store/StoreTx/SpaceRef, PgStore (своя tx / savepoint, set_config, SpaceMismatch), IdentityHub/SpaceHandle/Retention, фикстуры hub/space · дальше P2.2
2026-09-26 · P2.2 · done · outbox: emit() с event_seq/sequence, pg_notify при коммите, scrub в SpaceHandle._emit · дальше P2.3
2026-09-26 · P2.3 · done · apply_region_change (CAS/version, ретрай, денормализация, события), StateMixin: set_lifecycle/retire/override/_observe/_command/_expire; SpaceBase в _space.py; D-107, D-108 · дальше P2.4
2026-09-26 · P2.4 · done · register: prepare_identity (валидация+шифрование до tx), идемпотентность, глобальная уникальность, связи, начальные регионы, profile_encrypted (миграция 0002); scripts/check.sh; D-109..D-111 · дальше P2.5
2026-09-26 · P2.5 · done · set_labels/set_tags/add_tags/remove_tags/set_attributes/update_profile (в т.ч. зашифрованный)/set_note/delete (RESTRICT), identity_data load/save с версией · дальше P2.6
2026-09-26 · P2.6 · done · link/unlink с событиями, links_of, link_summary (один запрос), impact_of (рекурсивный CTE с защитой от циклов) · фаза 2 закрыта, дальше P3.1
2026-09-26 · P3.1 · done · Pool/LeaseTiming (G-1.2), candidate_query (for_operation, теги, метки, профиль, among list/Select), pick_candidate (FOR UPDATE OF SKIP LOCKED, ленивое availability, D-007) · дальше P3.2
2026-09-26 · P3.2 · done · acquire: SKIP LOCKED + fencing + ih_leases/история одной tx, перезахват истёкшей с закрытием истории по expires_at, idempotency_key, wait с backoff, shared-режим, события lease.acquired/expired · дальше P3.3
2026-09-26 · P3.3 · done · pool.lease(): heartbeat на своей tx, самоограждение по monotonic (D-112), on_lost sync/async, LeaseLost, check(), release идемпотентный по token, счётчики в историю, LeaseContext · дальше P3.4
2026-09-26 · P3.3 · fix · коммит P3.3 прошёл при нестабильном падении (tail глотал код возврата); найден баг: on_lost не вызывался, если потерю первым заметил check(). Исправлено + регрессионный тест; протокол уточнён
2026-09-26 · P3.4 · done · lease.credentials()/session()/save_session() (fencing, StaleFencing), mark_authenticated, space.revoke_lease (fencing+1, история revoked, heartbeat → LeaseLost revoked) · фаза 3 закрыта, дальше P4.1
2026-09-26 · P4.1 · done · charge_quotas (savepoint, date_bin, WHERE used<limit), ключ операции в той же точке сохранения (D-114), pacing со сбросом (D-113), quota.exhausted раз за окно · дальше P4.2
2026-09-26 · P4.2 · done · lease.operation: свежий снимок + fencing, evaluate, списание до вызова, классификация вины (identity/session_invalid/banned/request), cool_down, error_score/карантин (D-115) · фаза 4 закрыта, дальше P5.1
2026-09-26 · P5.1 · done · add_proxy/add_proxies (fingerprint, шифрование), URL, здоровье: карантин по порогу, перепроверка с backoff ×2, dead, подтверждение выхода, override, вина прокси из операции; миграция 0003 (D-116) · дальше P5.2
2026-09-26 · P5.2 · done · sticky-привязка к наименее загруженному, ёмкость через bound_count (D-117, миграция 0004), wait/rebind, non-sticky, proxy.binding.changed/pool.exhausted, lease.proxy, proxy_id в истории · дальше P5.3
2026-09-26 · P5.3 · done · run_logins (одна попытка на аккаунт за прогон), LoginContext/linked (аренда связанного), challenge: открытие (зашифрованный контекст, один открытый), answer_challenge, expire_challenges; pytest-timeout 120 с · дальше P5.4
2026-09-26 · P5.4 · done · lease.verification: start (evidence зашифрован), submit/approve/reject/abandon/reconcile/evidence, verified_factors в той же tx, expire_verifications, требования платформы add/resolve · дальше P5.5
