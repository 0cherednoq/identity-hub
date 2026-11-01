# Ревью RFC «перепроверка аккаунта» и рекомендуемое решение

Статус: рекомендация к решению (не принято)  
Дата: 2026-11-01  
Версия кода: `identity-hub 1.0.0a7`, HEAD `6fa6a50` + незакоммиченный хотфикс `no_session → session_invalid`  
Входные документы: `docs/isue/auth-revalidation-architecture.md` (далее «ишью») и разбор Codex (далее «Codex»)  
Воспроизведение экспериментов: `docs/isue/auth-revalidation-experiments.py.txt` (скопировать в `tests/conformance/test_zz_exp_revalidation.py`, запускать с `pytest -s`)

---

## 0. Резюме

1. **Диагноз ишью верен, но шире, чем кажется.** Дефект не в одном переходе `no_session → session_invalid`, а в классе: результат провайдера (внешний факт) применяется как `Command`, а граф команд вправе отказать. Хотфикс закрывает одну клетку из двух падающих: `challenge → session_invalid` по-прежнему падает и оставляет операцию в `running` (эксперимент E1).
2. **Принимать ишью целиком нельзя.** Перестановка `sticky`/`confirm_leaving` противоречит решениям D-102/D-310; «каждый вызов получает durable terminal» недостижимо; плоский outcome и привязка к `request_id` без родительской попытки недостаточны.
3. **Codex прав в большинстве пунктов** (проверено по коду и экспериментами), но **переусложняет персистентность**: шифрованные артефакты и projection worker (его «RFC B») не нужны, если вердикты применяются как `Observation` (E6, прототип).
4. **Чего не заметили ни ишью, ни Codex:**
   - проба (`Observation`) сегодня способна затереть активный `challenge` (E3). Нужна защита, и она уже выражается существующим механизмом `sticky` (прототип E6);
   - `banned` + любое число наблюдений `good` всегда даёт `sticky`, а `confirm_leaving` для `banned` недостижим (E4): это мёртвая конфигурация, а не «два слабых наблюдения снимут бан»;
   - `login` допускается только для `lifecycle ∈ {provisioning, ready}`, `standing ∈ {good, limited, appealing}`, `auth ∈ {no_session, session_invalid}` (`auth.py:70-72`), поэтому «диагностический login» для `banned`/`retired`/`authenticated` невозможен без нового допуска;
   - recovery worker переводит зависшую операцию в `failed/claim_expired` и тем самым **теряет вердикт провайдера** (E5): он лечит зависание, но не восстанавливает факт.
5. **Рекомендация:** не удалять терминальные статусы и не расширять граф команд. Разделить три оси (наблюдение, допуск нагрузки, право на диагностику), применять все вердикты провайдера как `Observation`, добавить объявляемый на уровне сервиса диагностический допуск и тонкий оркестратор `auth.revalidate()` со структурным результатом. Разбан по наблюдению оставить ручным по умолчанию, с опциональной политикой подтверждений.
6. **Прототип подтвердил ядро решения** (раздел 6): после замены `Command → Observation` в двух местах и добавления `sticky={challenge, logging_in}` в `AUTH` все 10 клеток матрицы завершаются терминально, `challenge` не затирается, весь набор тестов (memory-бэкенд) проходит.

---

## 1. Методика и ограничения

- **Код.** Прочитаны `auth.py`, `sync.py`, `lease.py`, `service.py`, `domain/regions.py`, `domain/region_specs.py`, `api/identities.py`, `api/sync.py`, `store/base.py`. Репозиторий `checker_account` недоступен: всё про его поведение взято из текста ишью и не проверялось.
- **Эксперименты.** Запускались на текущем дереве и на чистом HEAD (через временный git worktree, затем удалён). Основной прогон: memory-бэкенд; матрицы E1, E5 повторены на PostgreSQL 16 (Docker) с тем же результатом. Прототип фикса проверен на memory-бэкенде; на PostgreSQL полный набор тестов с прототипом **не гонялся**.
- **Ресерч.** Три независимых прохода по первоисточникам через `WebFetch`. Важная оговорка: `WebFetch` возвращает пересказ страницы малой моделью, поэтому часть цитат может быть неточной. В тексте ниже использованы только факты, которые агенты сочли подтверждёнными; всё остальное помечено «не подтверждено». Для критичных решений стоит открыть ссылки глазами.
- Ничего в `src/` не изменено. Прототип жил в отдельном worktree.

---

## 2. Что показали эксперименты на текущем коде

### E1. Вердикт `session_invalid` для default-слота из каждого состояния `auth` (`verify_session`, `adopt_session`)

| Исходное `auth` | HEAD (без хотфикса) | Рабочее дерево (хотфикс) |
|---|---|---|
| `no_session` | `TransitionNotAllowed`, операция `probe:auth` остаётся `running` | OK, `session_invalid`, операция `succeeded` |
| `authenticated` | OK | OK |
| `session_invalid` | OK | OK |
| `challenge` | **`TransitionNotAllowed`, операция `running`** | **`TransitionNotAllowed`, операция `running`** |
| `logging_in` | OK | OK |

Оба метода ведут себя одинаково; memory и PostgreSQL совпадают. Причина: `verify_session` (`auth.py:456`) и `adopt_session` (`auth.py:605`) применяют вердикт как `Command`, а терминализация операции идёт в той же транзакции (`auth.py:464`), поэтому исключение графа откатывает и её.

Примечание по методике: `identities.create(session=...)` сразу выставляет `auth=authenticated`, поэтому чистый `no_session` нужно ставить явным `override`. Первая версия эксперимента этого не делала и показывала «OK» для `no_session`; исправлено.

### E2. Допуск обычной пробы (`sync.refresh`)

| Состояние | Результат |
|---|---|
| `ready` | проба выполнена |
| `banned` | проба выполнена, для `standing` итог `sticky` |
| `deleted` | проба выполнена, итог `sticky` |
| `retired` | `LeaseUnavailable: нет свободного аккаунта`, провайдер не вызван |
| `cooling` | то же |
| `retired + banned` | то же |

Следствия: предварительный `restore_standing()` для пробы **не нужен** (правило пробы по умолчанию разрешает любой `standing`, `service.py:319-327`); `activate()` для `retired` нужен, потому что в правиле только `{provisioning, ready}`, а `availability != available` блокирует независимо. Сообщение «нет свободного аккаунта» не говорит, какой фильтр сработал.

### E3. Проба поверх активного `challenge`/`logging_in`

`sync.refresh(regions=("auth",))` с вердиктом `session_invalid` **применяется** и затирает `challenge` и `logging_in` в `session_invalid` (результат `applied`). Для `logging_in` под живым lease проба не получит lease, но `challenge` lease не держит, поэтому реальный риск именно в нём: человек вводит код, а фоновая проба сбрасывает состояние.

### E4. `banned` и повторные наблюдения `good`

Три подряд `refresh(standing)` с вердиктом `good` на `banned`: каждый раз `sticky`, статус остаётся `banned`. Причина в порядке проверок: `sticky` (`regions.py:341`) срабатывает раньше `confirm_leaving` (`regions.py:345`), так что `BANNED ∈ confirm_leaving` (`region_specs.py:63`) недостижимо. Ишью верно заметило конфликт, но неверно прочитало последствия.

### E5. Воспроизведение инцидента целиком

`verify_session` из `challenge` → `TransitionNotAllowed`, операция `running`, провайдер вызван 1 раз. Повторный `verify_session` → `LeaseUnavailable: проверка session … уже выполняется`, провайдер второй раз не вызван. После истечения claim и `workers.provider_operations.run_once()` → `recovered: 1`, операция `failed/claim_expired`. Вердикт провайдера (`invalid`) при этом **потерян**: его нет ни в `result`, ни в `auth`. (Для PostgreSQL шаг recovery в этом прогоне не выполнялся: claim принудительно истекал только в memory-ветке.)

### E6. Прототип фикса (в изолированном worktree)

Изменения: (а) в `verify_session` и `adopt_session` `Command(SESSION_INVALID)` заменён на `Observation(SESSION_INVALID, source=PROBE)`; (б) в `AUTH` добавлено `sticky={challenge, logging_in}`, `sticky_exits={authenticated}`. Хотфикс графа не применялся.

- Матрица E1: все 10 клеток `OK`, операция `succeeded`; `challenge` и `logging_in` остаются на месте, итог для них `invalid` без смены `auth`.
- E3: проба поверх `challenge`/`logging_in` даёт `sticky`, состояние не затёрто.
- E5: повторный вызов проходит (`second: OK`), провайдер вызван 2 раза (первый вызов теперь завершается).
- Полный набор тестов на memory-бэкенде: без падений (2 пропуска из-за отсутствия `aiohttp` и `curl_cffi`).

Ограничения прототипа: не прогнан на PostgreSQL; `Observation` не переносит `retryable=False`, который нёс `Command`; `login` (`_set_auth`) не менялся (см. 5.5).

### Подтверждено чтением кода (без отдельного эксперимента)

- `login` ставит `logging_in` **до** вызова провайдера (`auth.py:908`) после коммита claim (`auth.py:904`).
- Технический отказ `login` (не `Blame.SESSION_INVALID`) переводит в `no_session` даже при старте из `session_invalid` (`auth.py:937-941`): последнее достоверное состояние теряется. Утверждение Codex верно.
- Адресное чтение `ctx.credential(kind, name)` не фильтрует `last_rejected_at` (`lease.py:581-586`); фильтр есть только в неадресных выборках (`lease.py:627`, `lease.py:656`). Утверждение Codex верно, предварительный `verify_credential()` для rejected+active не нужен. Для `disabled`, `revoked` и истёкшего credential адресное чтение вернёт `None`.

---

## 3. Что библиотека умеет сейчас без изменений

Ответ на первый вопрос: **частично**. Обход в checker можно сократить, убрать целиком нельзя.

| Обход checker | Нужен ли сейчас | Основание |
|---|---|---|
| `restore_standing()` перед пробой | **Нет**, лишний | E2: проба проходит для `banned`/`deleted` |
| `activate()` для `retired` | Да для пробы и login | E2; допуск login ещё уже (`auth.py:70-72`) |
| Объявить собственное правило `PROBE_OPERATION` в `Service` с `lifecycles=all` | Возможный обход без `activate()` для пробы | `service.py:319` добавляет правило только если его нет в `operations` (не проверено запуском); `availability` всё равно блокирует |
| `verify_credential()` перед login | **Нет**, если credential `active` и просто отвергнут | `lease.py:581-586` |
| `enable_credential()` | Да, только если credential стал `disabled` | адресное чтение вернёт `None` |
| Recovery worker раз в минуту | **Да, обязателен** | E5: без него зависшая операция живёт до истечения claim |
| Узнать, что провайдер сказал `good` про `banned` | Только по `RefreshResult.outcomes == "sticky"` и событию drift | E4 |

Оставшиеся реальные пробелы: диагностика при `paused/cooling/quarantined/retired` через `login`, одна идемпотентная операция «сессия, затем credential», видимость того, что библиотека *не применила*, и защита `challenge`.

---

## 4. Как это решают другие системы

Все ссылки в разделе 9. Пометка «не подтв.» означает, что исследователь не смог подтвердить факт первоисточником.

### 4.1. Три независимые оси: наблюдение, допуск, диагностика

| Система | Наблюдение | Допуск нагрузки | Проба при блокировке |
|---|---|---|---|
| Kubernetes | condition `Ready` (пишет kubelet/node controller) | `spec.unschedulable` (cordon), taints | kubelet и lease-heartbeat продолжаются независимо от cordon |
| Nomad | heartbeat узла | `eligibility`, `drain`; `-keep-ineligible` показывает, что оси разные | поведение проб при ineligible не подтв. |
| HAProxy | health (`fall`/`rise`) | admin state `MAINT`/`drain` | успешная проверка не должна возвращать `MAINT`-сервер в `UP` (по результату поиска, коммит не открылся) |
| Envoy | outlier detection (пассивно), active HC | ejection (временный) | **active HC продолжает работать для ejected-хостов** |
| Consul | passing/warning/critical, `success_before_passing` | maintenance = зарегистрированный critical check | ручной режим, выход только вручную |

Вывод: ни одна зрелая система не склеивает «что мы знаем», «можно ли использовать» и «можно ли проверить» в один enum. Это ровно проблема ишью.

### 4.2. Как выходят из блокировки

- **Автоматическая блокировка снимается автоматически по пробе, асимметрично.** Envoy: время ejection растёт кратно числу повторов, есть потолок `max_ejection_time` и предохранитель `max_ejection_percent`; `healthy_threshold` для active HC. HAProxy: `rise`/`fall`. Consul: `success_before_passing`/`failures_before_critical`. Hystrix: один пробный запрос после sleep window; resilience4j: окно из K проб. Scrapy-rotating-proxies: «мёртвые» прокси перепроверяются с рандомизированным экспоненциальным backoff.
- **Ручная блокировка снимается только человеком.** Kubernetes cordon, HAProxy `MAINT`, Consul maintenance, Polly `Isolated`, resilience4j `FORCED_OPEN`, Flux `suspend`. В circuit breaker ручное состояние **вообще не пропускает проб**.
- **Можно ли пробе снимать блок, настраивается явно.** Envoy: `successful_active_health_check_uneject_host` (по умолчанию да, отключается, «если HC не повторяет реальный трафик»). Прямой аналог в нашей задаче: политика `login_success_implies_standing_good` должна быть опцией сервиса.
- **Внимание: данные неоднозначны для `quarantined`.** Kubernetes и Envoy пробуют заблокированный ресурс; circuit breaker в forced-open не пробует. Для аккаунтов это решает смысл карантина (см. 6, слой L2).

### 4.3. Внешний ответ как факт, а не команда

- **Decider** (Chassaing): команда может быть отклонена (`decide`), а событие-факт «happened and there's nothing we can do to change the fact»; `evolve` не отклоняет события.
- **Terraform `plan -refresh-only`**: состояние подстраивается под реальный мир, желаемое не меняется.
- **Kubernetes**: `status` — «most recent observations of actual state», отдельный subresource, пишется отдельно от `spec`; reconcile идемпотентен и не зависит от события-триггера.
- **Библиотеки state machine не задают канона.** python-transitions по умолчанию бросает исключение (reject) и отключает его `ignore_invalid_triggers`; Spring отбрасывает неприменимые события; gen_statem в примерах игнорирует неизвестные; wildcard-переходы выражают always-accept. Явного канона «внешний факт против команды» в проверенных документах нет (не подтв.): это принятое в event sourcing различие, а не требование библиотек.

### 4.4. Результат вызова фиксируется отдельно от его применения

- **Temporal:** результат activity пишется в Event History «до» того, как его увидит код workflow; «Event History is durably persisted». Activity at-least-once, поэтому идемпотентна. Что ошибка детерминизма не теряет уже записанный результат, источник прямо не говорит (не подтв.).
- **Brandur/Stripe:** «atomic phases» между внешними мутациями; «if the call fails, our local state will still have a record of it happening that we can use to retry». Stripe сохраняет код и тело первого ответа по idempotency key, повтор возвращает сохранённый ответ; параметры сверяются.
- **IETF draft-ietf-httpapi-idempotency-key-header-08** (черновик, не RFC): повтор при живом оригинале — 409, тот же ключ с другим payload — 422.
- **Неизвестный исход:** Stripe прямо называет исход при 500/таймауте «indeterminate»; безопасная процедура — повторить с тем же ключом, затем сверить с источником истины; новый ключ до сверки использовать нельзя.
- **Outbox/Inbox** решают двойную запись в собственной БД, но **не** случай «внешний вызов состоялся, локальная транзакция откатилась»: для него нужна ранее закоммиченная запись результата. Для Step Functions формулировки «outcome unknown» не найдено (не подтв.).

### 4.5. Терминальность

- XState, Step Functions, Temporal: финал закрывает **запуск/актора**, а не мир; продолжение — новый run или явный `Reset` по команде оператора.
- OAuth: `invalid_grant` (RFC 6749 §5.2) покрывает и «отозван навсегда», и «истёк/неверный redirect». Google и Microsoft советуют не считать это неисправимым, а запускать повторную авторизацию; Microsoft разделяет «повтори позже» и «нужен человек».
- Stripe Connect: платформа не хранит «липкий» статус, а перечитывает `requirements`/`disabled_reason` (`account.updated`); решение о `rejected`/`reinstated` принимает внешний владелец.
- GitHub App: suspend обратим (`DELETE`), деавторизация необратима. Граница проходит по тому, **кто** принял решение, а не по названию статуса.

### 4.6. Сквозные выводы

1. Терминал ограничивает **команды и запуски**, но не получение новых фактов.
2. Проба независима от допуска нагрузки; ручной блок не снимается результатом пробы.
3. Выход из автоматического блока — по пробе с асимметричными порогами и backoff; из ручного — только человеком.
4. Результат внешнего вызова фиксируется раньше и независимо от его применения; «исход неизвестен» — отдельное состояние с повтором по тому же ключу.
5. Идемпотентность: ключ + сверка параметров + 409 для живого дубля.

---

## 5. Разбор ишью и ответа Codex

### 5.1. Что принять

| Тезис | Источник | Почему принимаю |
|---|---|---|
| Вердикт провайдера — `Observation`, `Command` только для локального намерения | ишью, Codex | E1/E6; Decider; Terraform refresh-only; K8s status. Прототип устранил падения |
| Отдельный `auth.revalidate()`, а не `login(force=True)` | ишью, Codex | `force` не говорит, что обходится; в 4.1 допуск и проба — разные оси |
| `login()` нельзя переиспользовать как есть | Codex | `logging_in` ставится до ответа (`auth.py:908`), отказ ведёт в `no_session` (`auth.py:937-941`) |
| Рекомендовать запуск recovery worker | оба | E5 |
| Тест на каждую пару «состояние → вердикт» | оба | E1 показал, что хотфикс закрыл только одну клетку |
| Не снимать `banned/deleted` при первой версии | Codex | политика безопасности, не технический баг; см. 5.3 |
| Структурированный результат вместо плоского `outcome` | Codex | `authenticated` и `banned` не взаимоисключающи |

### 5.2. Что отклонить

| Тезис | Источник | Почему отклоняю |
|---|---|---|
| Переставить `sticky` и `confirm_leaving` | ишью | Нарушает D-102/D-310 (банящий статус намеренно «липкий»). Реальный дефект в другом: `confirm_leaving` для `banned` мёртв (E4). Лечить явной политикой (5.3), а не перестановкой |
| «Каждый завершённый сетевой вызов получает durable terminal operation» | ишью | Невозможно при падении между ответом и коммитом. Stripe называет это «indeterminate». Реалистичный контракт: `outcome_unknown` + повтор по тому же ключу + сверка |
| `request_id` как достаточная идемпотентность | ишью | Защищает от локальных дублей, но не даёт exactly-once у провайдера. Нужен ключ, передаваемый провайдеру, если он его поддерживает (Stripe, IETF draft) |
| Хотфикс `no_session → session_invalid` как итоговое решение | текущая правка | Закрывает 1 из 2 падающих клеток (E1), закрепляет вердикт-как-команда |
| Расширять граф `Command` под внешние факты | ишью (косвенно) | Каждое новое состояние снова ломает проверку; граф должен оставаться строгим |

### 5.3. Что изменить

**Разбан по наблюдению.** Ишью предлагает «слабое `good` после `banned` — два подтверждения, успешный login — авторитетное доказательство». Codex предлагает не снимать вообще. Данные ресерча дают нюанс:

- Stripe/GitHub не держат «липкий» статус: перечитывают каждый раз. Но у них решение принимает внешний владелец, а наш `sticky` защищает от ложного `good` от нестабильного провайдера.
- Зрелые системы выходят из блокировки через **асимметричные пороги** (Envoy, HAProxy, Consul), а не абсолютным запретом.

Решение: по умолчанию остаётся текущее поведение (ручной разбан, drift фиксируется), но вводится **опциональная политика сервиса** `standing_exit: manual | confirmed(n)` и `login_success_implies_standing_good: bool = False`. Мёртвый `BANNED ∈ confirm_leaving` либо удалить, либо оживить этой политикой. Это решение на уровне D-NNN, а не молчаливое изменение.

### 5.4. Где я не согласен с Codex

1. **«RFC B» (шифрованные артефакты, projection worker, retention) избыточен.**
   - Сессия при успехе login уже хранится в зашифрованном хранилище сессий (`_write_session`), это не переход графа и упасть на `TransitionNotAllowed` не может. Её можно писать в той же транзакции, что и терминализацию операции.
   - Единственное, что может откатить транзакцию, — переход графа. Вердикты-`Observation` это устраняют (E6).
   - Остаточные источники отката — инфраструктурные сбои, их покрывают `outcome_unknown` и recovery worker.
   - Поэтому вместо проекционной инфраструктуры нужна дешёвая сетка (L1 ниже). Полную модель откладываю до появления реальной потребности.
2. **Отдельная родительская таблица попыток не обязательна.** Идемпотентность строится на ключе запроса у самих операций (`request_key = "{request_id}:probe"`, `"{request_id}:login"`); составной результат читается из них. Уточнение после проверки схемы: сейчас в `ih_provider_operations` такого поля **нет**, живая операция уникальна только по `(identity_id, kind)` (`postgres/models.py:1031`, индекс `uq_ih_provider_operations_live`), поэтому нужна nullable-колонка и миграция (текущая `SCHEMA_HEAD = 0039`). Таблица попыток оправдана, только если понадобятся ожидание попытки по таймауту или история попыток как сущность. Это спорное место, оставлено открытым.
3. **«Хотфикс маскирует ошибочную классификацию» верно лишь отчасти.** Он действительно маскирует, но Codex не заметил, что оставшаяся клетка `challenge` падает точно так же.

### 5.5. Что не заметили ни ишью, ни Codex

- Защита `challenge` от затирания пробой (E3) и её решение через `sticky` в `AUTH`.
- `login` по-прежнему применяет итог как `Command` (`_set_auth`, `auth.py:1150`). Это допустимо, пока login сам владеет `logging_in` под своим lease, но параллельное изменение (override оператора во время login) всё ещё может дать `TransitionNotAllowed` и зависшую операцию. Поэтому сетка L1 нужна и после L0.
- Recovery теряет вердикт (E5).
- Сообщение «нет свободного аккаунта» не называет сработавший фильтр: нужен типизированный blocker.
- Допуск `login` уже, чем допуск пробы (`auth.py:70-72`).

---

## 6. Рекомендуемое решение

Слои независимы и могут внедряться по порядку.

### L0. Вердикты провайдера применяются как `Observation` (малый PR, высокий эффект)

- В `verify_session` и `adopt_session` заменить `Command(SESSION_INVALID)` на `Observation(..., source=PROBE)` с `observed_at = await tx.now()`.
- В `AUTH` добавить `sticky={challenge, logging_in}`, `sticky_exits={authenticated}`: наблюдение не затирает интерактивные состояния локального процесса входа, но успешная аутентификация, пришедшая снаружи, их закрывает. Команды (в том числе таймерный `Command(NO_SESSION)` и сам login) `sticky` не затрагивает, так как проверка стоит только в `_apply_observation`.
- Хотфикс графа `no_session → session_invalid` откатить: граф остаётся строгим.
- Решить, что делать с потерянным `retryable=False` (в `Observation` его нет) и как отразить `challenge`-защиту в результате (исход `sticky` уже возвращается).
- Тесты: матрица E1 как регрессия на обоих бэкендах (включая PostgreSQL); проба поверх `challenge`; закреплённый (`pin`) регион получает drift, а не изменение.

### L1. Страховочная сетка: операция не остаётся `running` из-за проекции

- Терминализация провайдерной операции и проекция перестают быть единым «всё или ничего» для исключений проекции: при исключении в проекции отдельной транзакцией операция становится `failed/projection_failed` (с secret-free `result`, содержащим вердикт), исключение пробрасывается дальше.
- Принцип: «Brandur recovery points» и «Temporal: результат в историю до применения», но без новой таблицы.
- Открытый вопрос: допускает ли модель ревизий операции (D-220) повторную запись в терминальную операцию. Проверить до реализации.
- Recovery: `claim_expired` → переходить в `retryable`/повтор по тому же ключу, а не терять вердикт; ввести явный `outcome_unknown` для крайнего окна «ответ получен, коммита нет».

### L2. Объявляемый диагностический допуск (а не флаг на pool)

- В `Service` — `DiagnosticPolicy(lifecycles, availability, standing)`, применяемая **только** к явным адресным операциям (`actor` и `reason` обязательны), не к `refresh_many`/фоновой сверке.
- Рекомендуемые значения по умолчанию: `lifecycles = {provisioning, ready, retired}`, `availability = {available, paused, cooling}`, `quarantined` и `deleted` — только по явному разрешению сервиса. Основание: карантин часто означает «не трогать аккаунт у провайдера»; данные ресерча неоднозначны (4.2), поэтому решение отдаётся сервису.
- Допуск обязан применяться и при выборе кандидата, и при повторной проверке внутри lease (`lease.operation()`), иначе второй гейт снова заблокирует.
- Отказы типизируются: `Blocker(kind=lifecycle|availability|credential_missing|credential_expired|credential_disabled|credential_revoked|proxy|busy, …)`.
- Результат диагностики не меняет `lifecycle` и `availability` (retired остаётся retired).

### L3. `auth.revalidate()` как тонкий оркестратор

```text
revalidate(identity_id, *, credential=None, actor, reason, request_id) -> AuthRevalidationResult
  1. диагностический допуск (L2) → lease
  2. проверка default-сессии, если есть пригодная (verify)         → Observation
  3. если сессии нет/невалидна и credential пригоден → вызов authenticator
     без перевода auth в logging_in (маркер «идёт» = claim операции)  → Observation + запись сессии
  4. вернуть структурный результат
```

- Результат: `status`, `session_verdict`, `credential_verdict`, `applied: dict[region → Outcome]` (чтобы `sticky`/`pinned` были видны вызывающему: иначе checker не узнает про разбан), `blockers`, `operation_ids`, `retryable`, `challenge_id` (существующий активный challenge возвращается, не перезапускается).
- Идемпотентность: `request_key` операции `{request_id}:{шаг}` (новая nullable-колонка `ih_provider_operations` и миграция, см. 5.4); живой дубль → типизированный `in_progress`; тот же `request_id` с другими параметрами → ошибка; новый `request_id` → новая попытка. Если у `authenticator` есть поддержка, передавать стабильный provider idempotency key через контекст.
- Не переводить `auth` в `logging_in`: иначе потерянный вердикт и откат в `no_session` (5.1). Нужен внутренний рефакторинг `_login`; оценка: средний риск, это самый дорогой кусок.
- Checker-статусы (`alive`, `dead`, `bad_credentials`) остаются за приложением.

### L4. Политика выхода из `banned/deleted` (позже, по решению)

- Опции сервиса `standing_exit`, `login_success_implies_standing_good` (см. 5.3). По умолчанию поведение не меняется. Operator pin сохраняется всегда: пишется только drift.

### Что откладывается

- Шифрованные артефакты, projection worker, статусы проекции (RFC B).
- Родительская таблица попыток (до появления потребности).
- Любое изменение порядка `sticky`/`confirm_leaving`.

---

## 7. План внедрения и критерии готовности

| Фаза | Содержание | Критерий готовности |
|---|---|---|
| D-NNN | Зафиксировать решения: вердикт = `Observation`; допуск диагностики отдельно; политика разбана — по умолчанию ручная | запись в `DECISIONS.md` |
| L0 | `Command → Observation` в `verify_session`/`adopt_session`; `AUTH.sticky`; откат хотфикса графа | матрица E1 зелёная на memory и PostgreSQL; проба поверх `challenge` — `sticky`; полный набор зелёный |
| L1 | Сетка `projection_failed` и поведение recovery | инъекция исключения в проекцию не оставляет `running`; вердикт сохранён в `result` |
| L2 | `DiagnosticPolicy`, типизированные blockers, двойной гейт | матрица допуска lifecycle × availability × standing; `refresh_many` не затрагивает retired |
| L3 | `auth.revalidate()`, идемпотентность, структурный результат | два параллельных вызова с одним `request_id` дают ≤ 1 вызов провайдера; `banned` остаётся `banned`, результат показывает `sticky` |
| L4 | Политика разбана (по решению) | `banned` + N подтверждений при включённой политике; по умолчанию без изменений |
| Checker | Убрать `restore_standing()`; затем `activate()`, `verify_credential()`; оставить recovery worker и метрику | обход сведён к вызову `revalidate()` |

Обязательные тесты: матрица E1 (оба метода, оба бэкенда), E2 как матрица допуска, E3, E4 (в том числе с включённой опциональной политикой), E5 с инъекцией сбоя проекции, параллельный `request_id`, abandoned `logging_in`, активный и истёкший challenge, missing/expired/disabled/revoked credential, `pin` не скрывает drift.

---

## 8. Риски и непроверенное

- **PostgreSQL:** полный набор тестов с прототипом не гонялся; матрицы E1/E5 на PostgreSQL проверены только без прототипа.
- **Радиус влияния `AUTH.sticky`:** затрагивает все пути, применяющие наблюдения `auth`. Набор тестов на memory-бэкенде зелёный, но стоит поискать код, который сознательно полагается на то, что проба выводит из `challenge`.
- **Потерянный `retryable=False`** при переходе на `Observation`: оценить, читают ли его потребители.
- **Модель ревизий операции (D-220)** и допустимость «досрочной» терминализации для L1: не проверено.
- **Поведение `quarantined`:** решение по умолчанию (не пускать) основано на здравом смысле при неоднозначных данных ресерча.
- **Не подтверждено ресерчем:** запуск проб HAProxy в `MAINT`; поведение Nomad с пробами при ineligible; Kubernetes ServiceAccount token revocation; Argo CD; Consul/PagerDuty/feature flags; счётчик успешных проб в большинстве пулов; что ошибка детерминизма Temporal не теряет результат; формулировка «outcome unknown» для Step Functions.
- **Checker:** поведение `checker_account` не проверялось, только его описание в ишью.
- Первоисточники читались через пересказывающий `WebFetch`: цитаты стоит перепроверить перед внешней публикацией.

---

## 9. Источники

Kubernetes и планировщики
- https://kubernetes.io/docs/concepts/architecture/nodes/
- https://kubernetes.io/docs/concepts/scheduling-eviction/taint-and-toleration/
- https://kubernetes.io/docs/reference/node/node-status/
- https://kubernetes.io/docs/reference/kubectl/generated/kubectl_cordon/
- https://developer.hashicorp.com/nomad/docs/commands/node/eligibility
- https://developer.hashicorp.com/nomad/docs/commands/node/drain

Балансировщики и health checks
- https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/upstream/outlier
- https://www.envoyproxy.io/docs/envoy/latest/api-v3/config/cluster/v3/outlier_detection.proto
- https://www.envoyproxy.io/docs/envoy/latest/api-v3/config/core/v3/health_check.proto
- https://raw.githubusercontent.com/haproxy/haproxy/master/doc/management.txt
- https://developer.hashicorp.com/consul/docs/services/configuration/checks-configuration-reference
- https://developer.hashicorp.com/consul/commands/maint

Circuit breaker
- https://martinfowler.com/bliki/CircuitBreaker.html
- https://resilience4j.readme.io/docs/circuitbreaker
- https://www.pollydocs.org/strategies/circuit-breaker.html
- https://github.com/Netflix/Hystrix/wiki/How-it-Works

Дрейф и наблюдаемое состояние
- https://developer.hashicorp.com/terraform/cli/commands/plan
- https://fluxcd.io/flux/components/kustomize/kustomizations/
- https://kubernetes.io/docs/concepts/overview/working-with-objects/
- https://raw.githubusercontent.com/kubernetes/community/master/contributors/devel/sig-architecture/api-conventions.md
- https://github.com/kubernetes-sigs/controller-runtime/blob/main/FAQ.md
- https://www.kubebuilder.io/reference/good-practices

Долговечный результат, идемпотентность, неизвестный исход
- https://docs.temporal.io/activity-definition
- https://docs.temporal.io/workflow-execution/event
- https://docs.temporal.io/encyclopedia/detecting-activity-failures
- https://docs.aws.amazon.com/step-functions/latest/dg/concepts-standard-vs-express.html
- https://docs.aws.amazon.com/step-functions/latest/dg/concepts-error-handling.html
- https://brandur.org/idempotency-keys
- https://docs.stripe.com/api/idempotent_requests
- https://docs.stripe.com/error-low-level
- https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/
- https://microservices.io/patterns/data/transactional-outbox.html
- https://microservices.io/patterns/communication-style/idempotent-consumer.html
- https://debezium.io/blog/2019/02/19/reliable-microservices-data-exchange-with-the-outbox-pattern/
- https://thinkbeforecoding.com/post/2021/12/17/functional-event-sourcing-decider

State machines и терминальность
- https://stately.ai/docs/final-states
- https://docs.aws.amazon.com/step-functions/latest/dg/state-succeed.html
- https://docs.temporal.io/workflow-execution
- https://www.erlang.org/doc/apps/stdlib/gen_statem.html
- https://github.com/pytransitions/transitions
- https://docs.spring.io/spring-statemachine/docs/current/reference/

Внешние учётные данные и аккаунты
- https://www.rfc-editor.org/rfc/rfc6749.txt
- https://datatracker.ietf.org/doc/html/rfc7009
- https://developers.google.com/identity/protocols/oauth2#expiration
- https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow
- https://docs.stripe.com/connect/handling-api-verification
- https://docs.stripe.com/api/accounts/object
- https://docs.github.com/en/rest/apps/apps#suspend-an-app-installation

Пулы, карантин, ручные переопределения
- https://raw.githubusercontent.com/TeamHG-Memex/scrapy-rotating-proxies/master/README.rst
- https://github.com/brettwooldridge/HikariCP
- https://pkg.go.dev/github.com/jackc/pgx/v5/pgxpool
- https://redis.io/docs/latest/operate/oss_and_stack/management/sentinel/
- https://developer.hashicorp.com/terraform/language/meta-arguments/lifecycle
- https://prometheus.io/docs/alerting/latest/alertmanager/
