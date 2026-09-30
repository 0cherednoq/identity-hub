# Identity Hub Admin: полный план реализации

Этот план доводит веб-приложение от текущих проектных документов до выпускаемого продукта:
типизированного Litestar API, React SPA, необходимых read-моделей и команд библиотеки, тестов,
wheel, Docker image и эксплуатационной документации.

Основной исполнитель: разработчик или автономный AI-агент. Статусы ведутся только в
[`PROGRESS.md`](PROGRESS.md). Порядок автономной работы описан в [`AUTONOMY.md`](AUTONOMY.md).

## 1. Результат и границы

После выполнения плана пользователь может:

- запустить админку одной командой из готового wheel;
- встроить её как Litestar plugin в своё приложение;
- развернуть готовый Docker image;
- безопасно просматривать несколько доступных пространств и все 14 страниц из
  [`ADMIN-PAGES.md`](ADMIN-PAGES.md);
- выполнять разрешённые операции только через публичные команды Identity Hub;
- исследовать оперативные и исторические метрики;
- продолжить прерванную разработку по чекпоинту без знания прошлой сессии.

В scope входят пакет `identity-hub-admin`, расширения публичных контрактов `identity_hub`,
миграции для данных админки, frontend, API, тесты, CI, упаковка и документация.

В scope v1 не входят собственное управление пользователями, встроенный OIDC provider,
WebSocket, мобильное приложение, произвольный конструктор дашбордов и прямое редактирование
таблиц PostgreSQL.

## 2. Исходное состояние и источники

На момент создания плана библиотека уже реализует основное доменное ядро, PostgreSQL store,
поиск, карточку аккаунта, аренды, события и вебхуки. Фактический статус задач `P*` всегда берётся
из [`docs/plan/PROGRESS.md`](../plan/PROGRESS.md). План админки может начинать каркас и UI до
завершения библиотеки, но конкретная задача не закрывается, пока её внешний prerequisite `P*`
не имеет статус `done`.

Продуктовые источники:

- [`ADMIN-PAGES.md`](ADMIN-PAGES.md): страницы, поля, метрики и действия;
- [`ADMIN-STACK.md`](ADMIN-STACK.md): выбранный стек, сборка и запуск;
- [`FRONTEND-DESIGN-SYSTEM.md`](FRONTEND-DESIGN-SYSTEM.md): слои frontend, токены и компоненты;
- [`identity-hub-requirements.md`](../../identity-hub-requirements.md): доменные гарантии;
- [`docs/plan/IMPROVEMENTS.md`](../plan/IMPROVEMENTS.md): отложенные улучшения библиотеки;
- код и тесты: окончательная проверка фактического поведения.

## 3. Целевая структура

```text
admin/
├── pyproject.toml
├── README.md
├── hatch_build.py
├── Dockerfile
├── src/identity_hub_admin/
│   ├── api/                  # v1 controllers, DTO, errors, pagination
│   ├── application/          # use cases поверх публичного Identity Hub API
│   ├── auth/                 # principals, session, token, RBAC, CSRF
│   ├── static/               # production build SPA, генерируется
│   ├── app.py                # create_admin_app
│   ├── plugin.py             # IdentityHubAdminPlugin
│   ├── settings.py
│   └── cli.py
├── frontend/
│   ├── src/
│   │   ├── app/
│   │   ├── pages/
│   │   ├── features/
│   │   ├── entities/
│   │   ├── shared/
│   │   └── styles/
│   ├── tests/
│   ├── package.json
│   └── vite.config.ts
└── tests/
    ├── unit/
    ├── integration/
    ├── contract/
    └── e2e/
```

Код в `src/identity_hub/` остаётся частью библиотеки. Админка не импортирует внутренние модули
с префиксом `_` и не обращается к `identity_hub.postgres.models` или Store напрямую.

## 4. Сквозные контракты

### 4.1. API

- JSON использует `snake_case`, UUID как строки, время в UTC ISO 8601.
- Каждый ответ содержит `X-Request-ID`; ошибки следуют A-D010.
- Коллекции возвращают `items`, `next_cursor` и при дешёвом подсчёте `total`.
- Все списки задают стабильный tie-breaker по `id`.
- Фильтры валидируются на бэкенде и отражаются в URL frontend.
- Мутации поддерживают `Idempotency-Key`, а конфликт ревизии не перезаписывает свежие данные.
- OpenAPI генерирует TypeScript-клиент. Ручные DTO-копии во frontend запрещены.

### 4.2. Безопасность

- Любой endpoint объявляет минимальную роль и способ проверки пространства.
- Секретные ответы не кешируются (`Cache-Control: no-store`) и не попадают в общий query cache.
- Изменяющие действия требуют причину, если доменная команда её поддерживает.
- Показ секрета, экспорт, импорт и массовое действие аудируются.
- Данные одного пространства нельзя получить через идентификатор объекта другого пространства.

### 4.3. UI

- Каждая страница имеет loading, empty, error, stale и forbidden состояния.
- Статусы отображаются только централизованным `StatusBadge` и текстом, не одним цветом.
- Таблицы имеют клавиатурную навигацию, серверную пагинацию и URL-фильтры.
- Опасное действие показывает цель, последствия и требует подтверждения.
- Даты, числа, проценты, длительности и таймзоны форматируются shared-функциями.
- Светлая и тёмная темы проходят visual tests.

### 4.4. Общий Definition of Done

Для каждой задачи обязательны:

1. Тест сначала фиксирует ожидаемый контракт или регрессию.
2. Нет прямого SQL из пакета админки и ручных правок сгенерированного клиента.
3. Проверены авторизация, пространство, пустой набор, ошибка и конкурентное изменение.
4. Новые тексты и коды ошибок понятны без чтения исходников.
5. Применимые команды из `AUTONOMY.md` завершились с кодом `0`.
6. `PROGRESS.md` и при необходимости `DECISIONS.md` обновлены в том же коммите.

## 5. Задачи

### Фаза A0. Контракты, workspace и проверяемая среда

#### A0.1. Аудит исходных контрактов

Зависимости: нет.

- Сопоставить 14 страниц с существующими методами, DTO, событиями и таблицами библиотеки.
- Для каждого поля отметить: уже доступно, нужна read-модель, нужна команда, нужна миграция.
- Проверить ссылки и противоречия в трёх исходных admin-документах.
- Создать `docs/admin/CONTRACT-MATRIX.md` без копирования секретных полей.
- Все новые решения зарегистрировать как `A-D1xx`; изменения библиотеки также как `D-1xx`.

**DoD:** у каждого блока `ADMIN-PAGES.md` есть владелец данных и задача `A*`; нет поля со статусом
«неизвестно»; `progress_tool.py validate` проходит.

#### A0.2. Каркас отдельного пакета в workspace

Зависимости: A0.1.

- Создать `admin/pyproject.toml`, src-layout, frontend Vite-проект и lock-файлы.
- Настроить Python 3.12+, Litestar, pytest, ruff, mypy strict и task runner `poe`.
- Настроить Node 22, npm scripts, TypeScript strict, ESLint, Prettier и Vitest.
- Подключить локальную библиотеку Identity Hub для разработки без циклической зависимости.
- Добавить единые команды `dev`, `check-backend`, `check-frontend`, `test-e2e`, `build`.

**DoD:** чистая установка Python и Node зависимостей воспроизводима; минимальные Python и React
smoke tests проходят; корневой проект библиотеки продолжает собираться.

#### A0.3. Тестовая инфраструктура и CI-каркас

Зависимости: A0.2, P0.4.

- Поднять PostgreSQL 16 через testcontainers или существующий `IH_TEST_DATABASE_URL`.
- Создать фикстуры hub, двух пространств, трёх ролей и стабильного набора аккаунтов.
- Добавить HTTP integration client, fake clock, fake transports и factory fixtures.
- Создать Playwright-проект для Chromium, трасс и скриншотов при падении.
- Добавить CI jobs Python, frontend, contract и e2e с кешами lock-файлов.

**DoD:** тест из одного пространства не видит второе; CI запускается без внешних секретов;
диагностика падения содержит request ID и Playwright trace.

#### A0.4. Формальный контракт API

Зависимости: A0.2.

- Зафиксировать envelopes коллекций, ошибок и операций.
- Описать cursor, сортировку, фильтры, `Idempotency-Key`, `expected_revision` и `reason`.
- Создать общие DTO для статусов, временных окон, series и breakdown.
- Ввести стабильный реестр машинных error codes.
- Добавить contract tests OpenAPI: уникальные operation IDs, role/space metadata, отсутствие
  секретов в схемах и примерах.

**DoD:** пример controller генерирует детерминированную OpenAPI; негативные contract tests ловят
секретное поле, маршрут без роли и нестабильную пагинацию.

#### A0.5. Локальная среда и демонстрационные данные

Зависимости: A0.3, A1.1.

- Добавить безопасную dev-команду, которая создаёт демонстрационные пространства и состояния.
- Представить loading/empty/error/large dataset сценарии без реальных credentials.
- Запускать backend и Vite HMR одной командой, корректно завершать оба процесса.
- Описать первый запуск на Windows PowerShell и Unix shell.

**DoD:** новый разработчик открывает работающий dashboard по инструкции; повторный seed
идемпотентен; production не может случайно запустить seed.

### Фаза A1. Платформа backend и frontend

#### A1.1. Litestar app, настройки, CLI и раздача SPA

Зависимости: A0.2.

- Реализовать типизированные настройки URL БД, KEK, auth, host, port, логов и base path.
- Создать `create_admin_app()`, `/health/live`, `/health/ready` и CLI.
- Реализовать безопасный SPA fallback и cache headers для `index.html` и hashed assets.
- Проверять версию библиотеки, ревизию БД и наличие production static при старте.
- Корректно открывать и закрывать hub, dispatcher и HTTP resources.

**DoD:** CLI `--help` работает без БД; readiness объясняет несовместимую схему; path traversal
невозможен; fallback не перехватывает `/api/v1/*`.

#### A1.2. Аутентификация, сессии, CSRF и RBAC

Зависимости: A0.4, A1.1.

- Реализовать `AdminPrincipal`, роли A-D006 и allowed spaces.
- Добавить bearer token, вход по password hash и ограниченный trusted-proxy mode.
- Хранить короткоживущую подписанную сессию, реализовать login/logout/me.
- Защитить cookie-мутации CSRF, установить security headers и rate limit на login/reveal.
- Исключить timing leak при неверном пользователе и пароле.

**DoD:** матрица role × endpoint покрыта тестами; CSRF и поддельные proxy headers отклоняются;
cookie flags проверены; auth errors не раскрывают существование пользователя.

#### A1.3. Ограничение пространства, actor и идемпотентность

Зависимости: A1.2.

- Создать единый dependency для разрешения `space_slug` и проверки доступа.
- Передавать actor и request ID во все команды и события.
- Добавить общий adapter идемпотентных мутаций и повтор результата по ключу.
- Проверять, что object ID действительно принадлежит указанному пространству.
- Запретить запрос без space context для неглобального endpoint.

**DoD:** подмена slug или UUID не раскрывает объект; повтор mutation не дублирует действие;
actor виден в аудите без bearer token или cookie.

#### A1.4. OpenAPI и сгенерированный TypeScript-клиент

Зависимости: A0.4, A1.1.

- Выбрать и зафиксировать `@hey-api/openapi-ts` или `orval`.
- Генерировать `openapi.json` и `frontend/src/shared/api/generated` одной командой.
- Обернуть transport: base path, credentials, request ID, abort signal, error envelope.
- Добавить CI drift check и запрет ручного редактирования generated-файлов.
- Сопоставить Pydantic enums с исчерпывающими TypeScript union types.

**DoD:** изменение DTO без регенерации падает в CI; frontend собирается только из generated
types; отменённый запрос не показывает ошибку пользователю.

#### A1.5. Дизайн-система и каталог `/_design`

Зависимости: A0.2.

- Реализовать токены, reset и темы из `FRONTEND-DESIGN-SYSTEM.md`.
- Добавить shadcn primitives только через CLI и журнал `shared/ui/CHANGES.md`.
- Реализовать page templates, `QueryState`, `DataTable`, `StatusBadge`, metric и chart wrappers.
- Создать status registries для всех доменных enum.
- Настроить ESLint boundaries и запреты на локальные визуальные стили.
- Заполнить `/_design` всеми вариантами, длинными данными и ошибочными состояниями.

**DoD:** ESLint ловит запрещённый стиль и нарушение слоя; visual snapshots светлой и тёмной темы
стабильны; неизвестный backend enum отображается безопасно и заметно.

#### A1.6. App shell, router, query cache и URL state

Зависимости: A1.2, A1.4, A1.5.

- Создать login, shell, navigation, space switcher, breadcrumbs и user menu.
- Настроить TanStack Router и typed search params.
- Настроить TanStack Query по A-D012 без кеширования секретов.
- Реализовать boundary для 401, 403, 404, 409/412, offline и неожиданной ошибки.
- Сохранять только безопасные пользовательские настройки темы и плотности.

**DoD:** deep link и back/forward восстанавливают фильтры; смена пространства инвалидирует
space-scoped cache; logout очищает весь cache; keyboard navigation работает.

#### A1.7. Наблюдаемость и обработка ошибок

Зависимости: A1.1, A1.6, P7.1.

- Добавить structured access logs с request ID, actor, route, space, status и latency.
- Не логировать query/body для login, secrets и import.
- Добавить backend metrics для запросов и frontend error reporting hook без жёсткой зависимости.
- Показать request ID в unexpected error UI и health diagnostics.
- Добавить graceful shutdown и таймауты внешних вызовов.

**DoD:** тестовый запрос прослеживается от browser header до backend log; scrubber удаляет
секреты; контролируемая ошибка не создаёт stack trace уровня error.

#### A1.8. Базовый E2E, accessibility и visual harness

Зависимости: A0.3, A1.6.

- Реализовать helpers входа трёх ролей и выбора пространства.
- Добавить axe checks основных layouts и keyboard-only smoke.
- Проверить светлую/тёмную тему и desktop/mobile viewports.
- Зафиксировать правило: screenshot обновляется только вместе с объяснением в коммите.

**DoD:** login → shell → logout проходит в браузере; viewer не видит action controls и получает
403 при прямом API вызове; критических axe violations нет.

### Фаза A2. Операционные страницы чтения

Каждая задача этой фазы включает недостающую публичную read-модель библиотеки, API endpoint,
generated client, UI и сквозные тесты. Бэкенд не делает прямой SQL.

#### A2.1. Обзор, пространства и страница пространства

Зависимости: A1.8, P6.2.

- Реализовать `list_spaces`, `space_overview`, `spaces_matrix`, `pool_capacity`, tier/factor и
  requirement counts.
- Добавить глобальный обзор, таблицу пространств и space dashboard.
- Поддержать сервисный разрез, ссылки из ячеек в отфильтрованные аккаунты и текущие тревоги.
- Исторические графики временно показывают явный placeholder до A4.6, без фиктивных данных.

**DoD:** числа сверяются SQL oracle-тестом библиотеки; allowed spaces применены до агрегации;
0, один и большой набор корректно отображаются.

#### A2.2. Поиск и таблица аккаунтов

Зависимости: A2.1, P6.1.

- Реализовать все фильтры `ADMIN-PAGES.md` §3.4, keyset pagination и стабильную сортировку.
- Добавить настраиваемые безопасные колонки и URL-presets.
- Показывать региональные статусы, пригодность, blockers, аренду и прокси без секретов.
- Добавить выбор строк как основу будущих bulk actions, не выполнять мутации в этой задаче.

**DoD:** фильтры переживают reload и ссылку; cursor не пропускает и не дублирует записи при
одинаковом времени; 10 000 строк не загружаются целиком в браузер.

#### A2.3. Карточка аккаунта: режим чтения

Зависимости: A2.2, P6.2.

- Расширить `identity_card` недостающими безопасными DTO и revision.
- Реализовать вкладки состояния, profile/data, доступов, сессий, верификаций, requirements,
  login/challenge, аренд, квот, прокси, связей, событий и логов.
- Загружать тяжёлые вкладки лениво и независимо.
- Показывать наличие секрета и metadata, но не его значение.

**DoD:** refresh конкретной вкладки не перезагружает всю карточку; удалённый или изменённый
аккаунт обрабатывается явно; секретные поля отсутствуют в network response и DOM.

#### A2.4. Страница аренд

Зависимости: A1.8, P6.2.

- Добавить live leases, окна 15m/1h/6h/24h, group by pool/service/holder и history cursor.
- Вычислять пересечение длительности аренды с окном и heartbeat lag на бэкенде.
- Реализовать таблицы метрик, живых аренд и истёкших незакрытых записей.
- Добавить графики acquired и lost+expired; пустые выборки подключить после A4.2.

**DoD:** граничные аренды учитываются ровно по пересечению; p95 проверен fixture-набором;
оперативная таблица обновляется без потери выбранных фильтров.

#### A2.5. Очередь «нужен человек»: режим чтения

Зависимости: A1.8, P5.4, P5.5.

- Реализовать объединённую read-модель challenge, requirements, manual review, drift и quarantine.
- Нормализовать deadline, urgency и reason для общей сортировки.
- Создать секции очереди и deep links в нужную вкладку аккаунта.
- Действия остаются disabled с понятной причиной до A3.3.

**DoD:** очередь отсортирована по срочности; истёкший challenge отличим от активного; данные
другого пространства отсутствуют во всех секциях.

#### A2.6. Страница прокси

Зависимости: A1.8, P5.2.

- Реализовать proxy overview, pool capacity, карточку прокси, привязанные аккаунты и историю.
- Показать status, error score, retry time, bound/cap и in-lease count.
- Добавить фильтры по pool/status/capacity и переход в аккаунт.
- Подготовить action slots для A3.4.

**DoD:** `bound_count` сверяется с bindings в тесте; URL маскирует userinfo; proxy secret и
пароль не возвращаются.

#### A2.7. Журнал событий

Зависимости: A1.8, P8.1.

- Реализовать фильтры type group, severity, service, identity, proxy, lease, actor и диапазон.
- Использовать keyset `(occurred_at, id)` и безопасный summary payload.
- Добавить detail drawer с redacted JSON и deep links.
- Экспорт перенести в A3.6.

**DoD:** новые события при пагинации не создают дублей; secret scrubber покрыт property tests;
фильтр «включая журнальные» работает по каталогу событий, а не строковому префиксу UI.

#### A2.8. Справочник сервисов

Зависимости: A1.8, P1.4.

- Экспортировать безопасное описание зарегистрированных `Service` из библиотеки.
- Показать операции, квоты, правила, верификации, tiers, codes, proxy policy и возможности.
- Добавить counts по доступным пространствам.
- Явно отмечать функции, которых у сервиса нет.

**DoD:** никакой callable или SecretStr не сериализуется; порядок детерминирован; новый enum или
operation отображается без изменения страницы.

#### A2.9. Состояние фоновых работ

Зависимости: A1.8, P8.3, P8.4, P9.1.

- Реализовать dispatcher cursors, lag events/seconds, delivery queues, maintenance runs и stale probes.
- Добавить thresholds и health classification на бэкенде.
- Показать последние ошибки redacted, время последнего успеха и backlog.
- Не объявлять процесс «здоровым», если данные о heartbeat отсутствуют.

**DoD:** stopped, delayed, retrying и healthy различаются тестами; страница обновляется каждые
10 секунд только при видимой вкладке.

#### A2.10. Уведомления и вебхуки: режим чтения

Зависимости: A1.8, P8.4.

- Реализовать списки Telegram channels, rules, webhook endpoints, notifications и deliveries.
- Показывать маски, severity, состояние, failure streak, next attempt и redacted target.
- Реализовать preview count правила без отправки.
- Подготовить формы и action slots для A3.8.

**DoD:** bot token и webhook secrets отсутствуют в API; журналы имеют cursor; preview не меняет
cursor dispatcher и не создаёт delivery.

### Фаза A3. Команды и операторские сценарии

#### A3.1. Единый command gateway

Зависимости: A1.3, A2.3.

- Создать backend adapter для actor, reason, expected revision, idempotency и доменных ошибок.
- Стандартизировать confirm DTO и dry-run для массовых действий.
- Добавить frontend mutation hooks, conflict dialog и targeted cache invalidation.
- Запретить optimistic state для критических доменных статусов.

**DoD:** двойной клик выполняет команду один раз; 412 показывает свежие данные и не теряет
введённую причину; viewer mutation отклоняется до вызова библиотеки.

#### A3.2. Lifecycle, state override и requirements

Зависимости: A3.1, P5.5.

- Реализовать допустимые lifecycle commands, state override/unpin/restore и resolve requirement.
- Формировать доступные действия по доменной машине, а не по списку во frontend.
- Требовать причину и показывать последствия sticky/pinned состояния.
- Обновить карточку аккаунта и human queue после успеха.

**DoD:** недопустимый переход нельзя выполнить через UI или прямой API; stale revision даёт 412;
аудит содержит actor/reason и не содержит секретов.

#### A3.3. Login, challenge и verification actions

Зависимости: A3.1, A2.5, P5.5.

- Добавить run login/resync, answer challenge, expire indication, approve/reject/abandon/reconcile.
- Показать инструкции сервиса, срок и подтверждение для ручного решения.
- Не хранить challenge answer после завершения запроса.
- Реализовать быстрый поток из human queue без потери позиции.

**DoD:** expired challenge не принимает ответ; повтор idempotent; verification attempts и revision
обновляются; answer/evidence отсутствуют в логах, кеше и trace.

#### A3.4. Аренды, прокси и связи

Зависимости: A3.1, A2.4, A2.6, P5.2.

- Реализовать revoke lease с причиной, proxy recheck/override/rebind и link/unlink.
- Выполнять impact analysis до unlink и показывать затронутые аккаунты.
- Проверять proxy capacity и конфликт параллельной перепривязки.
- Инвалидировать только затронутые summaries, cards и lists.

**DoD:** revoke соблюдает fencing; unlink с impact требует дополнительного подтверждения;
гонка rebind не ломает `bound_count`; все действия ограничены пространством.

#### A3.5. Показ и обновление секретов

Зависимости: A3.1, P5.6.

- Создать отдельные admin-only endpoints reveal credentials/session/profile/evidence.
- Требовать свежую повторную аутентификацию и явное подтверждение.
- Возвращать `Cache-Control: no-store`, автоматически скрывать значение по таймеру и при blur.
- Реализовать обновление credentials/session только через библиотеку.
- Исключить значения из query cache, clipboard telemetry, error reporting и snapshots.

**DoD:** reveal создаёт `identity.secret.revealed`; response не кешируется; viewer/operator
получают 403; browser back не восстанавливает значение.

#### A3.6. Массовые действия и экспорт

Зависимости: A3.2, A2.2, A2.7.

- Добавить server-side selection snapshot по фильтру, а не пересылку тысяч UUID.
- Реализовать dry-run, лимиты, частичный отчёт и идемпотентный batch ID.
- Поддержать безопасные массовые lifecycle/resync/tag actions, только если есть методы библиотеки.
- Экспортировать CSV/JSON событий и аккаунтов без секретов, формул и опасных CSV значений.

**DoD:** выбор «все по фильтру» не захватывает новые записи после preview; формулы CSV
экранируются; экспорт аудируется; частичный сбой можно безопасно повторить.

#### A3.7. Импорт и отчёты запусков

Зависимости: A3.1, A4.1, P5.7.

- Реализовать drag-and-drop CSV/JSONL, клиентскую подсказку и серверную строгую валидацию.
- Создать import run, background batches, progress, cancel boundary и итоговый redacted report.
- Поддержать service, `on_existing`, verify sessions, batch labels и links.
- Не сохранять исходный файл; ограничить размер, записи, поля и время разбора.

**DoD:** рестарт продолжает незавершённый run без дублей; плохая строка имеет номер и безопасную
причину; отчёт не содержит credentials/session; e2e импортирует смешанный файл.

#### A3.8. Управление уведомлениями и вебхуками

Зависимости: A3.1, A2.10, P8.4.

- Добавить CRUD channels, rules и endpoints с проверкой URL/filter/template.
- Реализовать rotate secret, send test, preview, enable/disable и manual retry.
- Маскировать targets согласно роли; секрет показывать только один раз при создании/ротации.
- Показать retry policy и последствия включения endpoint.

**DoD:** SSRF policy проверена; test delivery отделена от production cursor; ротация сохраняет
окно перекрытия; ручной retry идемпотентен и аудируется.

### Фаза A4. Исторические данные и аналитика

#### A4.1. Миграции данных админки

Зависимости: A0.1, P9.1.

- Добавить новыми миграциями timezone пространства, import runs, нужные operation state и индексы
  из `ADMIN-PAGES.md` §5 после проверки актуальной схемы.
- Не добавлять таблицу, если тот же факт уже надёжно восстанавливается из событий.
- Обновить Store/read contracts и retention policy.
- Зафиксировать каждое изменение в `docs/plan/DECISIONS.md`.

**DoD:** upgrade работает с последней выпущенной ревизии и с чистой БД; downgrade strategy
описана; schema drift test и библиотечные проверки зелёные.

#### A4.2. События tiers и MetricsHook

Зависимости: A4.1, P9.3.

- Генерировать tier reached/lost атомарно с изменением факторов.
- Подключить MetricsHook к lease acquisition, wait, empty, operations и dispatcher lag.
- Определить ограниченную cardinality labels.
- Записать административные агрегаты через стабильный adapter, не замедляя critical path.

**DoD:** tier event не дублируется при повторе; hook failure не ломает доменную операцию;
секреты и identity UUID не становятся metric labels.

#### A4.3. Идемпотентные временные роллапы

Зависимости: A4.2, P8.1.

- Создать `ih_stats_rollup` с уникальным ключом metric/dims/bucket.
- Реализовать dispatcher consumer для 1m, 1h и 1d buckets с cursor.
- Заполнять пропуски нулями на чтении; учитывать timezone для daily buckets.
- Добавить rebuild команды по диапазону и retention минутных данных.

**DoD:** повтор одной пачки не меняет итог; late commit не теряется; rebuild совпадает с raw event
oracle; 30-дневный запрос не сканирует весь `ih_events`.

#### A4.4. Почасовые снимки состояния

Зависимости: A4.1, P9.1.

- Создать `ih_state_snapshots` и maintenance job по space/service/region/state.
- Сделать запись идемпотентной на час и безопасной при двух workers.
- Реализовать retention и backfill текущего состояния без выдуманной истории.
- Отмечать неполный historical coverage в API.

**DoD:** сумма снимка совпадает с текущим срезом в момент fixture; два workers не создают дубль;
UI может отличить «0» от «данных ещё нет».

#### A4.5. Read-модели аналитики

Зависимости: A4.3, A4.4.

- Реализовать event series, funnel, time-to, cohorts, ban rate, utilization и proxy quality.
- Валидировать разрешённые metrics, dimensions, bucket и range.
- Ограничить число точек и групп; добавить cache validators для исторических диапазонов.
- Сопоставить каждую формулу определению из `ADMIN-PAGES.md` §2 и §3.7.

**DoD:** golden dataset проверяет все формулы и timezone/DST boundaries; неизвестная dimension
отклоняется; максимальный ответ ограничен и документирован.

#### A4.6. Страница аналитики

Зависимости: A4.5, A1.5.

- Реализовать группы поступления, авторизации, верификации, standing, нагрузки, качества данных и
  прокси.
- Поддержать окно, bucket, dimension и сравнение с предыдущим периодом в URL.
- Реализовать accessible chart wrappers, таблицу данных и export image/data без секретов.
- Показывать coverage, timezone и точное определение метрики рядом с графиком.

**DoD:** все графики имеют табличную альтернативу; URL воспроизводит вид; пустые buckets равны 0,
а отсутствие coverage отмечено отдельно; visual tests покрывают длинные legend.

#### A4.7. Тревоги и исторические блоки обзора

Зависимости: A4.5, A2.1.

- Подключить сравнение 24h, lease windows, всплеск банов и dispatcher lag.
- Реализовать backend alert evaluation с code, severity, evidence и target link.
- Не дублировать notification engine; dashboard alerts являются read projection.
- Заменить placeholders A2.1 реальными графиками.

**DoD:** alert threshold детерминирован и протестирован; каждое предупреждение ведёт к
отфильтрованным данным; stale analytics помечается, а не показывается как свежая.

### Фаза A5. Укрепление, упаковка и выпуск

#### A5.1. UX, accessibility и адаптивность

Зависимости: A3.2, A3.3, A3.4, A3.5, A3.6, A3.7, A3.8, A4.6, A4.7.

- Пройти все 14 страниц клавиатурой, screen-reader labels и axe.
- Проверить desktop, tablet и mobile urgent flows.
- Выправить focus management, reduced motion, contrast, truncation и empty/error copy.
- Проверить 0, 1, большие числа, длинные идентификаторы и неизвестные enum.

**DoD:** нет critical/serious axe violations в основных потоках; критические действия доступны
на мобильном; visual suite утверждён в обеих темах.

#### A5.2. Производительность и устойчивость

Зависимости: A5.1, P10.3.

- Установить бюджеты API p95, JS bundle, first load, table interaction и analytics query.
- Нагрузить 100k accounts, большие events, 50 concurrent viewers и delivery backlog.
- Добавить индексы только по результатам `EXPLAIN (ANALYZE, BUFFERS)`.
- Проверить abort, timeout, retry, DB unavailable и graceful degradation.

**DoD:** бюджеты и результаты записаны в `docs/admin/PERFORMANCE.md`; нет N+1; frontend chunks
разделены по маршрутам; деградация не создаёт retry storm.

#### A5.3. Аудит безопасности

Зависимости: A5.2.

- Создать threat model для auth, space isolation, CSRF, XSS, SSRF, import, export и secret reveal.
- Запустить dependency audit, secret scan и активные негативные integration tests.
- Проверить headers, cookie, CSP, CORS, redirect и path traversal.
- Проверить, что ошибки, traces, screenshots и frontend cache не содержат секретов.

**DoD:** high/critical findings устранены; разрешённые риски явно приняты в `DECISIONS.md`;
security regression suite входит в CI.

#### A5.4. Wheel, CLI и Litestar plugin

Зависимости: A5.3.

- Реализовать hatch build hook и включение hashed static в wheel.
- Собрать standalone CLI и `IdentityHubAdminPlugin` с base path.
- Проверить wheel в чистом Python-окружении без Node и исходников frontend.
- Показать версии admin, library и DB revision в health/UI.

**DoD:** wheel содержит `index.html`; `uvx ... --help` и server smoke работают без Node;
встроенный base path корректно обрабатывает API, assets и deep links.

#### A5.5. Docker и deployment profiles

Зависимости: A5.4.

- Создать multi-stage Dockerfile без Node и build secrets в runtime image.
- Запускать non-root, добавить healthcheck, read-only filesystem guidance и graceful stop.
- Подготовить compose example с PostgreSQL и mounted KEK secret.
- Документировать standalone, reverse proxy TLS и embedded deployment.

**DoD:** image запускается с read-only root filesystem и non-root user; healthcheck отражает DB
readiness; scanner не находит high/critical исправимых уязвимостей.

#### A5.6. Release CI и supply chain

Зависимости: A5.5.

- Добавить матрицу Python 3.12–3.14 и Node 22 для проверок.
- Собирать wheel/image один раз, затем тестировать и публиковать тот же digest.
- Генерировать SBOM, checksums и provenance, проверить содержимое wheel.
- Добавить version compatibility test и release dry-run без публикации.

**DoD:** dry-run создаёт воспроизводимые артефакты; установка wheel не вызывает npm; публикация
требует защищённого tag/environment и не запускается в обычном PR.

#### A5.7. Эксплуатационная документация

Зависимости: A5.5.

- Написать quickstart, configuration reference, auth setup и upgrade guide.
- Добавить runbooks для schema mismatch, dispatcher lag, delivery backlog, lost KEK access,
  failed import и rollback app version.
- Описать backup/restore фактов админки вместе с БД Identity Hub.
- Добавить troubleshooting по request ID без раскрытия секретов.

**DoD:** команды проверены на чистом окружении; у каждой настройки есть тип, default, secret flag
и пример; оператор может диагностировать readiness failure по runbook.

#### A5.8. Финальный аудит и release candidate

Зависимости: A5.6, A5.7, P10.5.

- Создать `docs/admin/TRACEABILITY.md`: страница/действие/риск → API → тест.
- Проверить все 14 страниц, три роли, два пространства, обе темы и все способы запуска.
- Выполнить полный backend, frontend, contract, e2e, visual, load и security suite.
- Удалить временные flags/placeholders; зафиксировать известные ограничения.
- Поднять версию до `1.0.0rc1`, собрать, установить и проверить артефакты.

**DoD:** матрица без пробелов; все обязательные проверки зелёные; release notes перечисляют
миграции, совместимость, ограничения и rollback; `PROGRESS.md` показывает все задачи `done`.

## 6. Граф зависимостей и параллельная работа

Критический путь:

```text
A0.1 -> A0.2 -> A0.4 -> A1.1 -> A1.2 -> A1.3 -> A1.4 -> A1.6 -> A1.8
     -> A2.1 -> A2.2 -> A2.3 -> A3.1 -> A3.2..A3.8
     -> A4.1 -> A4.2 -> A4.3/A4.4 -> A4.5 -> A4.6 -> A4.7
     -> A5.1 -> A5.2 -> A5.3 -> A5.4 -> A5.5 -> A5.6/A5.7 -> A5.8
```

После A1.8 страницы A2.2–A2.8 можно разрабатывать независимо, если их prerequisites `P*` готовы.
После A3.1 независимы state, verification, proxy/link, secrets и integrations. A4.3 и A4.4 также
можно выполнять параллельно. Один исполнитель всё равно берёт задачи в порядке
`progress_tool.py next`, чтобы не держать несколько незавершённых контуров.

## 7. Контроль покрытия страниц

| Страница | Read slice | Actions / history |
|---|---|---|
| Обзор | A2.1 | A4.7 |
| Пространства | A2.1 | A4.7 |
| Пространство | A2.1 | A4.7 |
| Аккаунты | A2.2 | A3.6 |
| Карточка аккаунта | A2.3 | A3.2–A3.5 |
| Аренды | A2.4 | A3.4, A4.2 |
| Аналитика | A4.5 | A4.6 |
| Нужен человек | A2.5 | A3.2, A3.3 |
| Прокси | A2.6 | A3.4 |
| Журнал событий | A2.7 | A3.6 |
| Импорт | A4.1 | A3.7 |
| Уведомления и вебхуки | A2.10 | A3.8 |
| Сервисы | A2.8 | только чтение |
| Фоновые работы | A2.9 | диагностика и runbooks A5.7 |
