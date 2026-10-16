# Identity Hub — план исправлений по ишью server-admins

Источник требований: [`../isue/09-assessment.md`](../isue/09-assessment.md) — аудит ишью;
тексты ишью — [`../isue/server-admins/`](../isue/server-admins/) (копия
`template2/server-admins/docs/issues/identity-hub` от 2026-10-14).
Прогресс: [`SERVER-ADMINS-PROGRESS.md`](SERVER-ADMINS-PROGRESS.md).
Решения: [`DECISIONS.md`](DECISIONS.md) (`D-310`–`D-322`).
Протокол автономной работы: [`CLAUDE.md`](../../CLAUDE.md).

Этот файл — спецификация задач `S<фаза>.<номер>`. Статусы ведутся только в
`SERVER-ADMINS-PROGRESS.md`. Изменение объёма, разбиение или отмена задачи оформляются записью
`D-NNN`, затем синхронно отражаются в плане, матрице трассировки (раздел 8) и прогрессе.

## Откуда план

`server-admins` встроил `identity_hub==1.0.0a2` и прислал восемь ишью. Аудит `09-assessment.md`
сверил их с кодом и разделил так:

| № | Ишью | Вердикт аудита | Задачи |
|---|---|---|---|
| 01 | Причина неудачного входа не сохраняется | обосновано, P1 | S1.1 |
| 02 | Сводка `logins.failed` без `code` | дефект, P0 | S0.1 |
| 03 | У `Fault` нет срока standing | пробел API, P2 | S2.1, S2.2 |
| 04 | Наблюдение выводит аккаунт из `deleted` | дефект, P0 | S0.2 |
| 05 | Замена credentials не сбрасывает auth-код | реально, автосброс спорен, P2 | S3.1, S3.2 |
| 06 | `cool_down` из `quarantined` запрещён | поведение верное, P3 | S3.1 (только документация и тест) |
| 07 | Сигналы репутации при входе теряются | обосновано, P1 | S1.2 |
| 08 | Пример публичного `Observation` неверен | уже исправлено | не выполняется |

Запросы владельца, добавленные к очереди: временный бан (`S2.2`, `D-311`), открытое хранение
credentials и сессий (фаза `S5`, `D-313`), стандартные форматы сессий и credentials
(фаза `S6`, `D-314`), приёмка импортированных аккаунтов (фаза `S7`, `D-317`). Фазы добавлены
после закрытия `S4.1`; `S5`–`S6` закрывает `S6.6`, `S7` — `S7.2`.

## Границы

- Код поведения `quarantined → cooling` не меняется (ишью 06): карантин строже охлаждения.
- Безусловного сброса `auth` при замене credentials нет (ишью 05): только явный параметр.
- Классификатор ошибок операций (`error_classifier`) к входу **не применяется**: существующие
  классификаторы писались для операций, и включение их для входа поменяло бы поведение молча.
  Адаптер входа сообщает исход явным `LoginFailure` (`D-310`, п. 3).
- `login_many` не меняет выбор кандидатов: аккаунт с неповторяемым отказом входа он
  по-прежнему пробует. Приложение фильтрует по коду отказа в карточке (`D-310`, п. 4).
- Срок standing разрешён только там, где автомат его снимает: `limited` (`S2.1`) и `banned`
  (`S2.2`, временный бан — `D-311`). `frozen`, `appealing` и `deleted` срока не принимают.
- Миграции не нужны фазам `S0`–`S3` и `S6`: код региона `auth` уже хранится в
  `ih_region_states.code`. Миграция есть только в `S5.1`. Номер — следующий свободный после
  `0032` (её заняла `I4.3`); если задаче `S0`–`S3` всё же понадобится
  колонка — так же и запись `D-NNN`.

---

## 0. Правила реализации

Действуют правила раздела 0 `IMPROVEMENTS-PLAN.md`, `INTEGRATION-PLAN.md` и `CONSOLE-PLAN.md`
(падающий тест первым, оба бэкенда, документация и CHANGELOG в том же коммите). Дополнительно:

1. **Регрессионный тест первым.** Для дефектов (`S0.*`) тест воспроизводит сценарий ишью через
   публичный API (`identities.observe`, `auth.login`, `analytics.series`) и падает до исправления.
2. **Оба бэкенда.** Поведение проверяется conformance-тестом на `MemoryStore` и PostgreSQL
   (маркер `pg`); доменные правила — ещё и unit-тестом автомата.
3. **Безопасность данных.** В события, строку региона, `ih_provider_operations` и логи попадает
   только машинный `code` (≤ `CODE_LIMIT`, через `sanitize_text`). Текст исключения, логин,
   пароль, ответ платформы — никогда. На каждый новый путь — тест, что секрет из исключения не
   попал ни в событие, ни в операцию.
4. **Совместимость.** Поле `data["error"]` событий входа остаётся; обычные исключения адаптера
   ведут себя как раньше (`auth → no_session`, операция `failed`), меняется только то, что
   записано в `code`.
5. **Состав событий.** Новое поле в `data` — сразу в таблицу `docs/guide/10-events.md` (её
   сверяет плагин тестов, `C4.3`).
6. **Коммиты.** Старт — `plan: start S<x.y>`; закрытие — один коммит: `fix(S<x.y>): …` для
   `S0.*`, `feat(S<x.y>): …` для остальных; в теле — номер ишью (`server-admins #02`).
7. **Гейт.** Во время задачи — целевые тесты; полный `bash scripts/check.sh` — перед закрытием
   последней задачи фазы и в `S4.1` (указание владельца: полный прогон один раз в конце серии).
   Наборы со временем (аренда, heartbeat) — 3 прогона.

---

## Фаза S0 — дефекты (P0)

### S0.1 Измерение `code` у `logins.failed`

Ишью 02. `auth.py::_login` пишет `data={"error": name}`, `rollups.py::event_contributions`
читает `data["code"]`: строка сводки `logins.failed` есть, измерения `code` нет.

- Первым — тест: вход через настоящий `Authenticator.login`, бросающий исключение, затем
  `analytics.series("logins.failed", group_by="code")` возвращает группу с этим кодом
  (сейчас — пустая группа). Событие строится реальным путём, не вручную.
- Все места, пишущие `identity.login.failed`, кладут `data["code"]`: `_login` (имя класса через
  `safe_error_code`, для таймаута — `LoginTimeout`), `_restore_auth` (имя причины прерывания),
  `_expire_logins` (`LoginAbandoned`). `data["error"]` остаётся с тем же значением.
- `rollups.py`: `code = data.get("code") or data.get("error")` — для событий, уже лежащих в
  журнале до исправления (пересчёт сводок по старой истории даёт те же группы).
- `10-events.md`: у `identity.login.failed` поля `code`, `error`.
- **DoD:** каждый путь неудачного входа (исключение, таймаут, прерывание, брошенный вход) даёт
  непустой `code`; сводка группируется по коду на обоих бэкендах; старое событие без `code`
  попадает в группу по `error`.

### S0.2 `deleted` терминален для наблюдений

Ишью 04. У `Standing.DELETED` нет командных выходов, но `_apply_observation` таблицу переходов
не проверяет, а `deleted` нет ни в `sticky`, ни в `confirm_leaving`. Запоздавшее наблюдение
возвращает удалённый аккаунт в работу.

- Первым — тесты: unit автомата (`deleted` + наблюдение `limited`/`good`/`banned` → состояние
  `deleted`) и conformance через `identities.observe()`; плюс путь операции: `Fault(Blame.IDENTITY_BANNED,
  standing="limited")` для аккаунта в `deleted` не меняет standing.
- Исправление: `STANDING.sticky = {BANNED, DELETED}` (`sticky_exits` остаётся `{DELETED}`).
  Наблюдение другого состояния → `Outcome.STICKY` и событие `identity.state.drift` с причиной
  `sticky` — тот же механизм, что у `banned` (`D-102`). Отдельного множества терминальных
  состояний не вводить, если `sticky` хватает; иначе — запись `D-NNN`.
- Выход из `deleted` — только `override` с причиной (проверить, что `_apply_override` его
  пропускает, и закрепить тестом). Пробы и сверка (`sync`) идут через наблюдения — проверить,
  что они тоже получают `STICKY`, а не исключение.
- Проверить все места, где код опирается на «наблюдение меняет `deleted`» (`grep DELETED`,
  тесты автомата, админка): исправить или оставить с записью причины.
- `04-states.md`: `deleted` конечен и для наблюдений; выход — только ручной правкой.
- **DoD:** два одинаковых запоздавших наблюдения не выводят из `deleted`; drift-событие
  записано; `override` с причиной выводит; оба бэкенда.

---

## Фаза S1 — исход входа (P1)

Ишью 01 и 07 описывают одну границу API: сейчас единственный особый исход
`Authenticator.login` — `LoginChallenge`, любое другое исключение — безымянный отказ.

### S1.1 `LoginFailure`: код отказа в карточке, событии и операции

Ишью 01.

- Публичный тип в `protocols.py` (экспорт из пакета, allowlist публичного API, `api.md`):
  ```python
  class LoginFailure(Exception):
      def __init__(self, code: str, *, retryable: bool = True,
                   fault: Fault | None = None, raw_status: str | None = None) -> None: ...
  ```
  `code` — машинный: `[a-z][a-z0-9_.-]{0,63}`, иначе `ValueError` в конструкторе (ошибка
  программиста видна в тестах адаптера). `str(exc)` не содержит ничего, кроме кода.
- Рекомендуемые коды — константы `LoginFailureCode` (`StrEnum`): `credentials_rejected`,
  `transient`, `session_invalid`, `identity_limited`, `identity_banned`. Свои коды разрешены.
- Обработка в `_login` (отдельная ветка перед общим `except Exception`):
  - `auth → no_session` с `code` в строке региона (`Command(..., code=failure.code)`);
  - событие `identity.login.failed`: `data = {"error": "LoginFailure", "code": ..., "retryable": ...}`;
  - операция `login`: `error_code=failure.code`, `result={"outcome": "failed", "retryable": ...}`;
  - возвращаемое сообщение `LoginResult` — `LoginFailure: <code>`, без текста исключения.
- Код отказа виден приложению: проверить, как карточка/`hub.read`/админка показывают код
  региона `auth`. Если в `Identity` его нет — добавить `auth_code` чтением строки региона
  (без новой колонки); если без колонки нельзя — миграция и `D-NNN`.
- Следующий вход (`no_session → logging_in`) очищает код — проверить тестом (так работает
  `Command` со сменой состояния).
- Документация: `05-login.md` — раздел «Неудачный вход», таблица кодов, пример адаптера.
- **DoD:** `credentials_rejected` виден в карточке, событии и операции на обоих бэкендах;
  обычное исключение ведёт себя как в `S0.1`; секрет из сообщения исключения не попал никуда;
  невалидный код — `ValueError`.

### S1.2 `LoginFailure.fault`: репутация при входе одной транзакцией

Ишью 07. Зависит от `S0.2` (`deleted` не должен оживать и через вход) и `S1.1`.

- Применение `Fault` вынести из `operation.py::_on_failure` в общую функцию (например,
  `domain`-независимый помощник в `operation.py` или новом модуле `faults.py`), которая в
  переданной транзакции меняет error score/availability, `auth` и `standing`. Её вызывают и
  операция, и вход. Поведение операции не меняется — существующие тесты операции зелёные.
- Вход с `LoginFailure(fault=...)`: исход операции `login`, событие отказа и последствия `Fault`
  пишутся **в одной транзакции** итога входа (`_set_auth`). Потерянная аренда (поздний ответ)
  откатывает всё вместе.
- Сопоставление: `Blame.IDENTITY_BANNED` → наблюдение standing (`fault.standing`, по умолчанию
  `banned`); `Blame.IDENTITY` → error score и cooling по `retry_after`; `Blame.SESSION_INVALID`
  → `auth → session_invalid` вместо `no_session`; `Blame.REQUEST` → только код;
  `Blame.PROXY` → отчёт о здоровье прокси своей транзакцией, как у операции.
- Источник наблюдения — `ChangeSource.ERROR`, `actor` — держатель аренды; `code` и
  `raw_status` проходят `sanitize_text`.
- **DoD:** бан при входе меняет standing и пишет `identity.standing.changed`; аккаунт в
  `deleted` остаётся `deleted` (`STICKY`); `retry_after` при входе ставит cooling; поздний ответ
  ничего не применяет; оба бэкенда.

---

## Фаза S2 — срок standing и временный бан (P2)

### S2.1 `Fault.until`

Ишью 03. Зависит от `S1.2` (общее применение `Fault`).

- Поле `until: datetime | None = None` у `Fault`. Проверка в `__post_init__` (`ValueError`):
  дата с часовым поясом; `until` только при `blame=IDENTITY_BANNED` и целевом standing, у
  которого автомат снимает срок (`STANDING.on_expire`, сейчас только `limited`).
- `until` попадает в наблюдение standing — и из операции, и из входа (через общую функцию).
- Повтор того же `limited` с новым сроком продлевает его, как сейчас делает наблюдение
  (`until=obs.until or cur.until`) — закрепить тестом.
- Истечение: после `until` таймер возвращает `limited → good` — тест на обоих бэкендах.
- `ErrorClassifier`, вернувший невалидный `Fault`, — это исключение классификатора: оно уже
  логируется, и ошибка считается виной запроса. Закрепить тестом, описать в `02-services.md`.
- **DoD:** одна неудачная операция с `Fault(IDENTITY_BANNED, standing="limited", until=...)`
  даёт `limited` со сроком без второго вызова `observe`; `banned`/`frozen` со сроком и наивная
  дата — `ValueError`.

### S2.2 Временный бан

Запрос владельца (`D-311`): бан «до такого-то времени», как в играх. Зависит от `S2.1`.

Сейчас у `banned` нет выхода по таймеру: `STANDING.on_expire` содержит только `limited`, а
записанный к бану `until` ничего не снимает. Бессрочный бан должен остаться бессрочным.

- Автомат: `STANDING.on_expire = {LIMITED: GOOD, BANNED: GOOD}`. Таймер (`_apply_timer`)
  срабатывает только при заданном `until`, поэтому бан без срока таймер не трогает. Закрепить
  тестом: `banned` без `until` после любого `now` остаётся `banned`.
- Снятие по сроку не требует причины (`requires_reason` проверяется только для команд):
  `change_source=TIMER`, событие `identity.standing.changed` с `from=banned`, `to=good`.
  Проверить, что поля события позволяют отличить истечение бана от ручного `restore_standing`;
  если нет — добавить в `data` признак и обновить `10-events.md`.
- Как задать срок:
  - `Fault(Blame.IDENTITY_BANNED, until=...)` (по умолчанию `standing=banned`): проверка из
    `S2.1` разрешает `until` для `banned`, для `frozen` по-прежнему `ValueError`;
  - публичное `identities.observe(Observation(state=banned, until=...))` — тот же путь;
  - ручной бан со сроком: `override(..., until=...)`, если `Override` уже принимает срок; иначе
    добавить параметр. Без `until` — бессрочный.
- Правила сочетания (тесты на каждое):
  - временный бан остаётся `sticky`: запоздавшее наблюдение `good` или `limited` до срока —
    `STICKY` и `identity.state.drift`, бан не снимается;
  - повторное наблюдение `banned` с более поздним `until` продлевает срок;
  - повторное наблюдение `banned` **без** `until` срок не отменяет (как у `limited`:
    `until=obs.until or cur.until`). Сделать бан бессрочным — только `override` без `until`.
    Если это окажется неудобным для адаптеров — записать `D-NNN` и решить по-другому;
  - `banned → deleted` по-прежнему разрешён и до срока (`sticky_exits`);
  - закреплённый (`pinned`) standing таймер не снимает (`R2-F14`).
- Хранилище: выборка истёкших standing уже строится по `STANDING.on_expire` (`PgStore`,
  `MemoryStore`), индекс `ix_ih_identities_standing_until` частичный по `standing_until IS NOT
  NULL` — миграция не нужна. Проверить тестом обслуживания на обоих бэкендах, что бан со
  сроком снимается, а бессрочный не попадает в выборку.
- Карточка и админка показывают срок бана (`standing_until`); если админка не выводит срок для
  `banned` — вывести.
- `04-states.md`: диаграмма `banned --> good : истёк until`, таблица «временный и бессрочный
  бан», как задать срок из адаптера и вручную. `limitations.md` — убрать ограничение, если есть.
- **DoD:** `Fault(IDENTITY_BANNED, until=now+1d)` даёт бан, который обслуживание снимает после
  срока; бан без срока не снимается никогда; наблюдение `good` до срока бан не снимает; оба
  бэкенда.

---

## Фаза S3 — credentials и охлаждение (P2/P3)

### S3.1 Документировать замену credentials и `cool_down` из карантина

Ишью 05 (вариант 1) и 06.

- Тесты, закрепляющие текущее поведение:
  - `secrets.replace_credentials()` не меняет регион `auth` и его код, не отзывает сессию;
  - `identities.cool_down()` для `quarantined` → `TransitionNotAllowed`, состояние и срок не
    меняются; повтор для `cooling` только продлевает срок;
  - `Fault(Blame.IDENTITY, retry_after=...)` и `op.cool_down()` для аккаунта в карантине не
    бросают и карантин не ослабляют (`_set_availability` молча оставляет более тяжёлое
    состояние) — проверить и закрепить фактическое поведение.
- `04-states.md` и `03-accounts.md`: где применим `cool_down`, почему карантин не переводится в
  охлаждение, чем снимается карантин; что замена credentials не трогает `auth` и сессию, и
  рецепт для приложения (запустить вход сразу после замены или `S3.2`).
- **DoD:** тесты зелёные на обоих бэкендах; разделы руководства есть.

### S3.2 Явный сброс кода входа при замене credentials

Ишью 05 (вариант 2). Зависит от `S1.1` и `S3.1`.

- `secrets.replace_credentials(..., reset_auth: bool = False)`. При `True` в той же транзакции,
  что замена: если `auth` в `no_session` или `session_invalid` — `auth → no_session` без кода.
  `authenticated`, `logging_in`, `challenge` не трогаются (идёт или выполнен вход).
  Сессия не отзывается.
- Нужен способ очистить код при том же состоянии (`no_session` с кодом → `no_session` без
  кода): у `Command` сейчас `code=None` значит «оставить». Ввести явный признак очистки
  (например, `Command(clear_code=True)`) с событием `identity.auth.changed`; записать `D-NNN`.
- Идемпотентность `replace_credentials` (`idempotency_key`) учитывает `reset_auth`.
- Админка: если в ней есть замена credentials — передавать `reset_auth=True` (проверить
  `admin/`; если формы нет — не трогать).
- **DoD:** после `replace_credentials(reset_auth=True)` карточка не показывает
  `credentials_rejected`; без флага поведение `S3.1`; оба бэкенда; под `using(session)` откат
  приложения откатывает и сброс (`pg`).

---

## Фаза S4 — закрытие

### S4.1 Финальная сверка

- `tests/test_server_admins_traceability.py` по образцу `test_console_traceability.py`: ишью
  01–08 из `docs/isue/server-admins/` есть в матрице раздела 8; каждая задача плана есть в
  прогрессе; ишью 08 помечено «не выполняется».
- `CHANGELOG.md` (раздел Unreleased), `docs/guide/api.md`, `limitations.md`, allowlist
  публичного API совпадают с кодом.
- В конец `docs/isue/09-assessment.md` — раздел «Итог реализации»: ишью → задача → что
  сделано → что приложению `server-admins` можно убрать из своих обходов.
- Полный `bash scripts/check.sh` зелёный; наборы со временем — 3 прогона.
- **DoD:** все задачи `S0`–`S3` в статусе `done` или `cancelled` с записью `D-NNN`.

---

## Фаза S5 — открытое хранение credentials и сессий

Запрос владельца (`D-313`): читать credentials и сессии прямо из базы в читаемом виде. Сейчас
секреты лежат в `ih_credentials` и `ih_sessions` шифром AES-GCM в бинарных колонках
(`EncryptedPayloadMixin`: `key_id`, `wrapped_dek`, `nonce`, `ciphertext`, все `NOT NULL`).
Флаг «не шифровать» в `Envelope` задачу не решает: `bytea` в `SELECT` не читается.

Границы:
- открыто хранятся **только** `ih_credentials` и `ih_sessions`; прокси, вебхуки, каналы
  уведомлений, staged-импорт и зашифрованный профиль остаются под шифром;
- мастер-ключ нужен всегда: без него не прочитать остальные секреты и не вывести ключ
  отпечатков;
- read-схема (`READ_TABLES`) секретов не получает; доступ к двум таблицам — правами PostgreSQL;
- по умолчанию ничего не меняется: `secret_storage="encrypted"`.

### S5.1 Режим `secret_storage="plaintext"`: схема, запись, чтение

- Миграция (следующий свободный номер после `0032`, `D-012`): у `ih_credentials` и
  `ih_sessions` колонка `payload jsonb NULL`; колонки шифра становятся `NULL`; `CHECK`: заполнен
  ровно один формат — `payload` либо все четыре колонки шифра. Модели `postgres/models.py`
  и `MemoryStore` — так же.
- Настройка `IdentityHub(secret_storage="encrypted" | "plaintext")`; неизвестное значение —
  `ValidationFailed` при старте.
- Запись: в режиме `plaintext` credentials и сессия пишутся в `payload` JSON-ом модели схемы
  сервиса (`model_dump(mode="json")`, `SecretStr` раскрывается). Остальные секреты — как прежде.
- Чтение понимает оба формата независимо от режима: в одной базе могут лежать зашифрованные и
  открытые строки.
- Идемпотентность (`idempotency_key`, `D-208`): HMAC-тег считается тем же ключом отпечатков,
  что и сейчас; формат хранения на тег не влияет.
- Лимит размера секрета (512 KiB) действует и для `payload`.
- Метаданные без значений (`public_value`, `expires_at`, `status`) не меняются.
- **DoD:** в режиме `plaintext` запрос `SELECT payload->>'password' FROM ih_credentials`
  возвращает пароль; `lease.credentials()`, `lease.session()`, `secrets.reveal` и импорт
  работают в обоих режимах на обоих бэкендах; смешанная база читается; в режиме `encrypted`
  поведение и данные прежние (существующие тесты зелёные).

### S5.2 Перевод хранения, предупреждения и документация

Зависит от `S5.1`.

- Команда `identity-hub secrets convert --to plaintext|encrypted [--space SLUG] [--batch N]`
  и метод `hub.doctor.convert_secrets(to=...)`: пачками перекладывает строки двух таблиц в
  нужный формат, по образцу `rotate-key`; повтор безопасен; прерывание не портит строки.
  Ревизии и версии сессий не меняются, событий домена нет; в журнал администратора
  (`admin_audit`) — запись с числом строк.
- Предупреждение при старте хаба и в `identity-hub doctor`, если режим `plaintext` или в базе
  есть открытые строки: «credentials и сессии хранятся открытыми».
- `02-services.md` и `03-accounts.md`: режим, что хранится открыто, пример SQL, права доступа к
  таблицам, риск (дамп базы выдаёт все аккаунты), перевод в обе стороны. `limitations.md`,
  CHANGELOG.
- **DoD:** `convert --to plaintext`, затем `--to encrypted` возвращает базу к шифру без потери
  данных (оба бэкенда, тест на 1000 строк); предупреждение видно в логе и в `doctor`.

---

## Фаза S6 — стандартные форматы сессий и credentials

Запрос владельца (`D-314`). Исследование и спецификация моделей и конвертеров —
[`../research/SESSION-FORMATS.md`](../research/SESSION-FORMATS.md) (далее — «исследование»);
задачи ссылаются на его разделы. Хранение и миграции не меняются: модели ложатся в
существующие `credentials`, `credential_kinds`, `session` и `profile` сервиса.

Общие правила фазы:
- **Публичный API — неймспейсы `dump` и `load` на моделях** (исследование, раздел 5.0;
  `D-314`, п. 6):
  - выгрузка: `session.dump.playwright()`, `session.cookies.dump.netscape()`;
  - загрузка: `BrowserSession.load.playwright(state)`, `Cookies.load.netscape(text)`;
  - автоопределение формата: `Cookies.load(data)`, `BrowserSession.load(data)`.
  
  Для обычной работы импортировать модули форматов не нужно.
- **Реализация — модули `identity_hub.formats.<формат>`** с функциями `dump` и `load`.
  Методы неймспейсов вызывают их в одну строку; логика форматов в моделях не живёт.
  Модули тоже публичные: ими пользуются для сырых данных без модели.
- **Неймспейсы — дескрипторы с точными типами** (`dump.playwright() -> StorageState`,
  `dump.cookiejar() -> CookieJar`, `load.netscape() -> Cookies`). У pydantic-моделей они
  объявляются как `ClassVar`, чтобы не стать полями. Объект `load` вызываемый: вызов без
  формата — автоопределение.
- На самой модели — только данные и операции над ними: `for_url`, `header_for`, `get`,
  `expires_at`. Методов `to_*`/`from_*` под форматы нет.
- **Живые инструменты** — методы аренды с ленивым импортом: `lease.playwright_context(browser)`
  и `lease.selenium_apply(driver)`. Они не конвертируют, а открывают контекст или добавляют
  cookie в браузер.
- **Никаких новых обязательных зависимостей**: `dump` возвращает `dict`, `list`, `str` и объекты
  stdlib. Модули форматов не импортируют Playwright, Selenium, httpx, aiohttp, curl_cffi.
- **Потери при конвертации** везде обрабатываются одинаково: Python-предупреждение
  `FormatLossWarning` (публичный класс), а `strict=True` превращает его в `ValidationFailed`.
  Тип результата от `strict` не меняется.
- Публичные имена — в allowlist и `api.md`.
- **Секреты.** Значения cookie, токенов и паролей не попадают в `repr`, логи и тексты ошибок —
  тест на каждую модель.
- **Образцы.** Золотые образцы реальных выгрузок — `tests/fixtures/formats/<формат>/`. При
  закрытии задачи у каждого её формата есть образец и круговой тест
  `X.load.<f>(X.dump.<f>()) == X` там, где формат без потерь (исследование, раздел 7).

### S6.1 Модели, нормализация и каркас `dump`/`load`

Исследование, разделы 4 (классификация — 4.3) и 5.0.

- **Модели:**
  - `Cookie`, `Cookies` (коллекция с ключом `(domain, host_only, path, name, partition_key)`),
    `SameSite`, `PartitionKey`;
  - сессии: `BrowserSession`, `OriginStorage`, `TokenSession` (OAuth 2.0 и OIDC:
    `authorization_header`, `is_expired`, `id_claims`), `OAuth1Session`, `HeaderSession`;
  - креды: `PasswordCredentials` (`basic_auth`), `OtpSecret` (TOTP и HOTP: `from_uri`,
    `to_uri`, `code` по RFC 6238 / RFC 4226 на stdlib), `RecoveryCodes` (`take`), `ApiKey`,
    `SigningKey`, `OAuthClient`, `OAuth1Client`, `PrivateKeyCredentials`, `ClientCertificate`;
  - классификация — по форме данных, а не по протоколу (исследование, раздел 4.3): отдельных
    типов для Basic, OIDC, CAS, SAML нет; модели не ходят в сеть, не проверяют подпись
    `id_token` и не подписывают запросы;
  - почта для восстановления — связанный аккаунт (`links`), модели `EmailAccess` нет;
  - `BrowserFingerprint` и вложенные `Screen`, `Viewport`, `WebGl`, `Geolocation`,
    `UserAgentData`.
- **Нормализация на входе:**
  - домен без точки и в нижнем регистре плюс `host_only`;
  - все представления сессионной cookie → `expires=None`;
  - `same_site` из всех диалектов;
  - `http_only` из `rest` без учёта регистра.
- **Проверки RFC 6265bis:** нарушение правил `__Host-` и `__Secure-` — ошибка; `SameSite=None` и
  partitioned без `secure` — предупреждение.
- **Схема сессии на слот:** сейчас `Service.session` одна на все слоты. Добавить
  `Service(session_slots={"api": TokenSession})`: слот без своей схемы использует
  `Service.session`; запись в слот проверяется его схемой на обоих бэкендах. Если это меняет
  контракт хранения — записать `D-NNN`.
- **Операции:** `Cookie.key`, `is_expired`, `matches(url)`; `Cookies.for_url(url)`,
  `header_for(url)`, `get(name, domain=, path=)`; `BrowserSession.expires_at(auth_cookies)`.
- **Каркас:**
  - базовые классы неймспейсов `DumpNamespace` и `LoadNamespace`;
  - `FormatLossWarning`;
  - модуль `identity_hub.formats` и первый формат на нём — `json`, каноническое представление
    самой модели (`Cookies.dump.json()` / `Cookies.load.json()`). На нём проверяются типы
    неймспейсов и `ClassVar` у pydantic.
- **DoD:**
  - модели работают как схемы `Service` на обоих бэкендах (создание, аренда, чтение);
  - TOTP и HOTP совпадают с тестовыми векторами RFC 6238 и RFC 4226;
  - `RecoveryCodes.take` не выдаёт израсходованный код;
  - сессия `BrowserSession` в слоте `default` и `TokenSession` в слоте `api` одного
    аккаунта сохраняются и читаются своими схемами;
  - тесты нормализации на все случаи из таблицы 3.2;
  - mypy видит точные типы `dump.json()` и `load.json()`;
  - сериализация модели не содержит `dump` и `load`.

### S6.2 Браузеры: `dump`/`load` для Playwright, CDP, Selenium, расширений и Crawlee

Зависит от `S6.1`. Исследование, раздел 5.1.

- `playwright`: cookies и `storage_state` (`sessionStorage` — предупреждение, `indexedDB` — как
  есть); `BrowserSession.dump.playwright()`, `Cookies.dump.playwright()`, обратные `load`.
- `cdp`, `puppeteer` (`partitionKey` — объект; у Puppeteer `sourceOrigin`); `extras`
  возвращаются при `dump.cdp()`.
- `selenium`: `expiry` целым, partitioned пропускаются с `FormatLossWarning`.
- `chrome_extension` (Cookie-Editor, EditThisCookie, AdsPower, GoLogin), `crawlee` (Python
  `CookieParam`), `tough_cookie` (Crawlee JS).
- Методы аренды `lease.playwright_context(browser, **options)` (контекст с отпечатком, прокси и
  сессией; при выходе — `storage_state` в `lease.save_session`) и `lease.selenium_apply(driver)`
  (группировка по хосту, переход на хост, `add_cookie`). Импорт Playwright и Selenium — внутри
  методов.
- **DoD:** образцы каждого формата загружаются; круговые тесты; необязательный интеграционный
  тест Playwright (`add_cookies` → `cookies()`) и `lease.playwright_context`, если пакет
  установлен (маркер, без новой обязательной зависимости).

### S6.3 HTTP-клиенты: `dump`/`load` для CookieJar, aiohttp, заголовков и Scrapy

Зависит от `S6.1`. Исследование, разделы 3.3 и 5.2.

- **`cookiejar`:** `Cookies.dump.cookiejar()` / `Cookies.load.cookiejar(jar)`. `Cookie` stdlib
  создаётся напрямую, не через `set()`; при загрузке разбирается `http_only: "True"/"False"`
  от curl_cffi.
- **`aiohttp`:** `Cookies.dump.aiohttp()` возвращает пары (`SimpleCookie`, `response_url`).
- **`set_cookie`:** `Cookies.load.set_cookie(headers, url=, received_at=)` — свой разбор, без
  `SimpleCookie`.
- **`scrapy`:** `Cookies.dump.scrapy()`.
- Строка `Cookie` — операция `Cookies.header_for(url)` из `S6.1`: RFC 6265 (домен, путь,
  secure, срок, длинный путь первым), а не формат.
- **Прокси** (исследование, 5.6): новое свойство аренды `lease.proxy` → `ProxyUrl | None` с
  неймспейсом `dump`: `playwright()`, `httpx()`, `requests()`, `aiohttp()`. `lease.proxy_url`
  остаётся строкой, как раньше. Реализация — модуль `formats.proxy`.
- **DoD:**
  - cookie уходит только на свой домен и путь — тест против локального HTTP-сервера для
    httpx, requests, curl_cffi, aiohttp (необязательный маркер, если пакеты установлены);
  - одно имя на двух путях не теряется;
  - host-only cookie не уходит на поддомен.

### S6.4 Файлы и автоопределение

Зависит от `S6.1`, `S6.2`. Исследование, разделы 5.3 и 5.5.

- **`netscape`:** `Cookies.dump.netscape(session_expires="zero"|"empty")` /
  `Cookies.load.netscape(text)` — свой читатель и писатель: `#HttpOnly_`, `0` и пустое поле →
  сессионная, терпимость к несовпадению точки и флага, CRLF.
- **`har`:** `Cookies.load.har(har)`.
- **Автоопределение:** `Cookies.load(data)` и `BrowserSession.load(data)` без формата
  распознают JSON-массив любого диалекта, `storage_state`, Netscape, строку `Cookie` и base64 от
  них. Неизвестное — `ValidationFailed` с перечнем ожидаемых форматов.
- **Строки продавцов** — только по шаблону: `formats.lines.load_account(line, template, sep)` и
  `formats.lines.load_proxy(line, template)`.
- **Не загружается никогда:** pickle и Chrome `Cookies` SQLite (шифр). Firefox
  `cookies.sqlite` — вне задачи.
- **DoD:** образцы curl, `MozillaCookieJar.save`, Cookie-Editor и EditThisCookie (Netscape и
  JSON), HAR загружаются; автоопределение верно распознаёт каждый образец.

### S6.5 Отпечаток: `dump`/`load` для генераторов и инструментов

Зависит от `S6.1`, `S6.2`. Исследование, раздел 5.4.

- `BrowserFingerprint.load.browserforge(fp)`, `load.fingerprint_suite(json)` (исходник — в
  `raw`).
- `BrowserFingerprint.dump.playwright_context(proxy=)`, `dump.camoufox()`, `dump.adspower()`,
  `dump.gologin()`.
- `BrowserFingerprint.dump.impersonate(client)` для `curl_cffi`, `tls_client`, `primp`, `rnet`:
  ближайшая доступная версия того же браузера не новее UA, `FormatLossWarning` при
  расхождении. Таблицы целей — данные модуля с указанием версии клиента, с которой они сверены.
- **DoD:**
  - образцы browserforge и fingerprint-suite загружаются;
  - выбор цели для набора UA (chrome 130/131/150, firefox, safari) проверен тестом;
  - неизвестный клиент — `ValidationFailed`.

### S6.6 Интеграция с хабом и документация

Зависит от `S6.1`–`S6.5`.

- **`Service(session_auth_cookies=frozenset[str])`.** При записи сессии типа `BrowserSession`
  (`lease.save_session`, `secrets.replace_session`, импорт) библиотека заполняет `expires_at`
  сессии минимальным сроком этих cookie, если вызывающий не передал свой.
- **Фильтр пулов по отпечатку.** `filterable_profile` сейчас проверяет только поля верхнего
  уровня профиля (`service.py`, `pool.py`). Либо поддержать путь `fingerprint.browser`, либо
  описать рецепт с плоскими полями профиля; выбор записать `D-NNN`.
- **Импорт.** `ImportRecord.sessions` принимает `BrowserSession` и результат `Cookies.load(data)`.
- **Руководство:**
  - раздел «Стандартные форматы» в `02-services.md`: таблица `dump.*`/`load.*` и потерь
    каждого формата;
  - рецепт «браузерный аккаунт» в `12-recipes.md` (пример из исследования, раздел 6);
  - CHANGELOG.
- **Закрытие фаз `S5`–`S6`:**
  - `tests/test_server_admins_traceability.py` покрывает новые задачи (строки «Запрос
    владельца» в матрице);
  - CHANGELOG и `limitations.md` сверены;
  - полный `bash scripts/check.sh` зелёный, наборы со временем — 3 прогона.
- **DoD:**
  - рецепт выполняется тестом на `MemoryStore`;
  - `expires_at` сессии выставляется по cookie;
  - документация сверена с allowlist публичного API;
  - все задачи `S5`–`S6` в статусе `done` или `cancelled` с записью `D-NNN`.

---

## Фаза S7 — приёмка импортированных аккаунтов

Запрос владельца (`D-317`): проверять импортированные аккаунты и пускать в работу только
валидные. Сейчас это собирается вручную (импорт → `provisioning`, свой воркер зовёт
`auth.login`, по исходу `activate`/`retire`). Фаза убирает ручной код: политика приёмки
задаётся декларативно, а применяет её библиотека.

Политика живёт **на вызове импорта**, со значением по умолчанию из сервиса, а не на аккаунте:
приёмка — свойство партии, а не отдельной записи. На `Service` — только дефолт, как у
`error_classifier` и `RetryPolicy`.

### S7.1 `AcceptPolicy`: модель, дефолт сервиса, постановка на приёмку

- `AcceptPolicy` (frozen dataclass, экспорт из пакета): `activate_on_login: bool = False`,
  `retire_on: frozenset[str] = frozenset()` (коды `LoginFailureCode` и значения standing, при
  которых аккаунт выводится, например `{"credentials_rejected", "identity_banned"}`),
  `max_attempts: int = 1`, `skip_on_challenge: bool = True` (ждущий человека аккаунт не трогаем).
- `Service.default_accept: AcceptPolicy | None = None`.
- `imports.run(..., accept: AcceptPolicy | None = None)` и `run_files(...)`: `accept` переопределяет
  `Service.default_accept`; `None` берёт дефолт сервиса; отсутствие дефолта — поведение как сейчас
  (аккаунты остаются в `provisioning`, приёмку ведёт приложение).
- **Исполнение — не в транзакции импорта.** Вход ходит на платформу, держать сетевой вызов в
  транзакции пачки нельзя. Аккаунты, созданные с непустой политикой, ставятся в очередь приёмки:
  переиспользуем механизм операций провайдера (`accept` как вид операции) или отдельную отметку
  «ожидает приёмки». Выбор — одна durable-отметка без N+1 — записать `D-317`; `accept=None`
  ничего не ставит.
- `verify_sessions` при непустой политике по умолчанию `True`: иначе аккаунт с сессией станет
  `authenticated` и вход его не проверит. Явный `verify_sessions=False` оставить, но
  предупреждать.
- **DoD:** `imports.run(accept=...)` помечает каждый созданный аккаунт на приёмку одной отметкой
  на пачку без N+1 (тест числа запросов на 10 и 1000); `accept=None` и отсутствие
  `default_accept` ничего не ставят; `AcceptPolicy` экспортирован и в allowlist; оба бэкенда.

### S7.2 `AcceptanceWorker`: применение политики, retryable, закрытие фазы

Зависит от `S7.1`.

- `hub.workers.acceptance.run_once(limit)` по образцу `ProviderOperationsWorker` (`D-223`): берёт
  аккаунты с ожидающей приёмкой в `provisioning` и `auth` ∈ {`no_session`, `session_invalid`},
  по одному под арендой (двойную проверку исключает аренда), вызывает `auth.login` и применяет
  политику прогона:
  - `SUCCEEDED` и `activate_on_login` → `activate`;
  - `FAILED` с кодом из `retire_on` (по `Identity.auth_code` или `standing`) → `retire` с причиной;
  - `FAILED` с временной ошибкой → повтор, пока попыток меньше `max_attempts`, затем снятие с
    очереди без активации;
  - `CHALLENGED` при `skip_on_challenge` → снять с очереди, оставить человеку.
  Вызывается из обслуживания, как provider-worker.
- **`retryable` в регионе `auth`.** Сейчас признак есть только в событии `identity.login.failed`.
  Сохранить его в строке региона (новой миграцией, если колонки нет) и научить `login_many` и
  приёмку **не брать повторно** аккаунты с последним отказом `retryable=False`, пока не заменят
  credentials. Если без миграции нельзя — номер после занятого `S5.1` и запись `D-317`.
- Идемпотентность и повторяемость: повторный запуск по уже принятому аккаунту ничего не меняет
  (он не в `provisioning` или снят с очереди); события — штатные `identity.login.*`,
  `lifecycle.changed`, `standing.changed`, сводного события приёмки нет.
- **Руководство:** рецепт «чекер импортированных аккаунтов» в `12-recipes.md` (строки
  `email pass`, сессии без логина, брокер и отдельный воркер); `02-services.md` — `default_accept`;
  `limitations.md`; CHANGELOG.
- **Закрытие фазы `S7`:** `tests/test_server_admins_traceability.py` покрывает `S7.*`; полный
  `bash scripts/check.sh` зелёный, наборы со временем — 3 прогона.
- **DoD:** партия из валидного, аккаунта с неверным паролем и забаненного после приёмки даёт
  соответственно `ready`, `retired` и `retired`; аккаунт с временной ошибкой остаётся в
  `provisioning` и принимается при повторном `run_once`; аккаунт с `retryable=False` повторно не
  логинится; `activate` порождает `lifecycle.changed → ready` для подписчика; оба бэкенда; все
  задачи `S7` в статусе `done` или `cancelled` с записью `D-317`.

---

## Фаза S8 — событие на каждый импортированный аккаунт

Запрос владельца (`D-318`): подписчик на `identity.registered` не видит аккаунты, пришедшие
импортом. `imports.run`/`run_files` пишут только сводное `identity.imported` со счётчиками
(`count`, `updated`, `skipped`, `run_id`), без идентификаторов созданных аккаунтов. Это
расходится с правилом очереди `C` «пакетная команда пишет то же событие на каждую сущность, что
и одиночная» (`create_many` пишет `identity.registered` на каждый аккаунт).

### S8.1 `imports.run` пишет `identity.registered` на каждый созданный аккаунт

- В транзакции пачки импорта (`importing.py`, рядом с текущим `identity.imported`) на каждый
  **созданный** аккаунт эмитируется `identity.registered` с тем же составом `data`, что у
  `create_many` (`external_ref`, `lifecycle`, `auth`, `tags`, `labels`, `links`). Обновлённые
  (`update_session`) и пропущенные аккаунты события не порождают.
- Сводное `identity.imported` остаётся: счётчики и `run_id` для наблюдения за прогоном.
- Порядок в транзакции: `identity.registered` на каждый аккаунт, затем одно `identity.imported`;
  подписчик на `registered` видит аккаунты импорта так же, как созданные через `create`.
- Проверить и закрепить тестом путь `create(lifecycle="ready")`: он должен давать событие,
  по которому подписчик узнаёт о готовом аккаунте (`identity.registered` с `lifecycle=ready`;
  `lifecycle.changed` при прямом создании в `ready` может не возникать). Если расхождение —
  описать в `10-events.md`.
- `10-events.md`: отметить, что `identity.registered` пишется и при импорте; таблица состава
  `data` (её сверяет плагин тестов, `C4.3`) не меняется.
- **DoD:** импорт `N` новых аккаунтов даёт `N` событий `identity.registered` плюс одно
  `identity.imported`; подписчик (`hub.events.subscribe(types=["identity.registered"])`) получает
  каждый импортированный аккаунт на обоих бэкендах; `update_session` и `skip` событий
  `registered` не порождают; `tests/test_server_admins_traceability.py` покрывает `S8.1`;
  CHANGELOG обновлён; полный `bash scripts/check.sh` зелёный.

---

## Фаза S9 — точечная валидность кредов/сессий и накопление при импорте

Запрос владельца (`D-319`). Два связанных разрыва:
1. У отдельной записи кредов нет признака «подошла/отклонена платформой»: `LoginFailure` и
   `Identity.auth_code` относятся ко всему аккаунту, пометить конкретную пару нельзя, хаб сам не
   метит. Нельзя взять в работу аккаунт «по валидной паре».
2. Повторный импорт того же аккаунта с другими кредами их отбрасывает (`SKIP`/`FAIL`) или трогает
   только сессию (`UPDATE_SESSION`). Накопить вторую пару импортом нельзя.

Обе части опираются на **отпечаток значения** (`FingerprintKeyring.tag`, HMAC без хранения
секрета, `D-207`): он даёт дедупликацию и сравнение «та же пара / новая пара» без расшифровки.

### S9.1 Отпечаток значения и признак валидности у записи кредов/сессии

- Миграция (следующий свободный номер после `0034`, `D-012`): в `ih_credentials` и `ih_sessions`
  — `value_fingerprint` (HMAC значения активным ключом отпечатков), `last_verified_at`,
  `last_rejected_at`, `reject_code` (ограниченный машинный код). Отпечаток пишется при каждой
  записи секрета. Ротация ключа отпечатков пересчитывает его там же, где `rewrap`.
- Канонизация значения перед отпечатком: `model_dump(mode="json")` схемы вида, ключи
  отсортированы, затем `tag`. Одинаковый секрет в разной форме даёт один отпечаток.
- Карточка (`cards.py`, `Credential`) показывает `last_verified_at`, `last_rejected_at`,
  `reject_code`; значения и отпечаток наружу не отдаются.
- **DoD:** у новой и перезаписанной записи есть отпечаток; два разных секрета дают разные
  отпечатки, одинаковые в разной форме — один; поля валидности видны в карточке, секрет и
  отпечаток — нет; оба бэкенда.

### S9.2 Пометка валидности из адаптера и учёт в приёмке

Зависит от `S9.1`.

- `ctx.reject_credential(kind, name=..., code=...)` и `ctx.verify_credential(kind, name=...)` —
  пометить запись из `Authenticator`/пробы в транзакции итога: `reject` ставит `last_rejected_at`
  и `reject_code`, по политике сервиса может сразу `disable`; `verify` ставит `last_verified_at`.
- `LoginFailure(credential=(kind, name), code=...)` — короткий путь: библиотека метит названную
  запись отклонённой тем же механизмом, атомарно с итогом входа.
- Адаптер, перебирающий записи, помечает проигравшие `reject`, победившую `verify`, даже когда
  вход в целом удался.
- Приёмка (`AcceptanceWorker`, S7) и `login_many`: кандидатом считается аккаунт, у которого есть
  хотя бы одна запись кредов нужного вида **не** в отказе (`last_rejected_at` пусто или сброшено
  заменой секрета). Аккаунт, у которого все пары отклонены, в повторный автоматический вход не
  берётся — смыкается с `retryable=False` (`D-317`).
- **DoD:** адаптер метит конкретную пару, в карточке видно какая валидна; `reject` опционально
  выключает запись; приёмка берёт аккаунт по валидной паре и не берёт, когда все отклонены; оба
  бэкенда.

### S9.3 Импорт с накоплением: режим `OnExisting.MERGE`

Зависит от `S9.1`.

Новый режим `imports.run(on_existing="merge")` — для существующего аккаунта **добавляет
недостающее, не трогая существующее**. Дедуп по отпечатку значения, по двум независимым осям.

**Креды** (ось `kind` + `value_fingerprint`):
- такой пары ещё нет → добавить новой записью. Имя: заданное в `credential_records`, если
  свободно; иначе авто-имя (детерминированное от отпечатка), чтобы не затирать существующую;
- такая пара уже есть (совпал отпечаток при том же `kind`) → **не добавлять**, в отчёт как
  дубль; если существующая запись была `rejected`, её замена отдельной командой, импорт её не
  воскрешает;
- `revoked`-запись с тем же отпечатком дубль не снимает (её нельзя переписать, `D-215`).

**Сессии** (ось `slot` + `value_fingerprint`):
- слота ещё нет → добавить;
- слот есть, отпечаток совпал → no-op;
- слот есть, отпечаток другой → по умолчанию **не затирать рабочую сессию**: если текущая
  `active`, входящую (непроверенную) не применять, записать в отчёт; если текущая `invalidated`
  или отсутствует — заменить (`mode="replace"`, прежняя в историю). Параметр
  `merge_sessions="keep"|"replace"` переопределяет: `replace` всегда ставит входящую новой
  ревизией.

**Разбор случаев «креды + сессия» у существующего аккаунта:**

| Пара кредов | Сессия слота | Итог |
|---|---|---|
| дубль | новая | добавить сессию, пару не добавлять |
| новая | дубль | добавить пару, сессию не трогать |
| новая | новая | добавить и пару, и сессию |
| дубль | дубль | ничего не менять |

- Всё — в транзакции пачки (хук `on_batch` работает как прежде). Аккаунт, которому что-то
  добавили, попадает в `report.updated`; полностью дублирующий — в `report.skipped`.
- `lifecycle`, `standing`, `auth` существующего аккаунта `MERGE` не меняет: это добавление
  данных, а не переприёмка. Валидность добавленных пар выставит следующий вход/приёмка (S9.2).
- Множественные сессии-кандидаты в одном слоте не вводятся: кандидаты сессий — это разные слоты;
  записать решением.
- **DoD:** повторный импорт той же пары ничего не добавляет; новой пары — добавляет запись;
  новая сессия к существующей паре добавляется, рабочая сессия не затирается непроверенной;
  `merge_sessions="replace"` заменяет; все четыре случая таблицы покрыты тестом на обоих
  бэкендах; `SKIP`/`UPDATE_SESSION`/`FAIL` ведут себя как раньше.

### S9.4 Документация и закрытие фазы

Зависит от `S9.1`–`S9.3`.

- `03-accounts.md`: несколько пар кредов, пометка валидности, режим `merge`; `12-recipes.md`:
  рецепт «много паролей-кандидатов на один аккаунт» (импорт `merge` → приёмка метит валидную);
  `10-events.md` — новые поля событий, если появились; `limitations.md`; CHANGELOG; allowlist
  публичного API.
- `tests/test_server_admins_traceability.py` покрывает `S9.*`.
- **DoD:** рецепт выполняется тестом на `MemoryStore`; документация сверена с кодом; полный
  `bash scripts/check.sh` зелёный, наборы со временем — 3 прогона; все задачи `S9` в статусе
  `done` или `cancelled` с записью `D-NNN`.

---

## Фаза S10 — выборки по связям аккаунтов

Запрос владельца (`D-320`): выбирать аккаунты по наличию/отсутствию связи — например, «твиттеры
в `ready`, на которые ещё не ссылается ни один privy» для кампании кросс-сервисной регистрации.
Сейчас связи аккаунт-аккаунт доступны только поштучно (`links.of`, `links.impact`); сет-выборок
нет, а `ih_identity_links` не в read-схеме, поэтому анти-join пишется сырым SQL к внутренней
таблице.

### S10.1 Связи аккаунтов в read-схеме

- Добавить в `READ_TABLES` (`postgres/read_schema.py`) таблицу `identity_links` →
  `ih_identity_links` с публичными колонками `identity_id`, `role`, `linked_identity_id`
  (и `created_at`, если есть). Экспонировать `hub.read.identity_links: Table`.
- Добавление таблицы в read-схему **не повышает** `READ_SCHEMA_VERSION` (правило read-схемы:
  новые колонки/таблицы добавляются без смены версии).
- Это стабильная поверхность для своих join/анти-join: «аккаунты сервиса `X` без связи роли `R`»
  пишется через `hub.read.identities` + `hub.read.identity_links` без обращения к внутренним
  таблицам.
- Read-схема — PostgreSQL (как и остальной `hub.read`); ограничение `MemoryStore` уже описано
  (`D-227`).
- **DoD:** `hub.read.identity_links` присутствует; анти-join (identities LEFT JOIN identity_links
  по роли, `linked_identity_id IS NULL`) возвращает верный набор на PostgreSQL; колонки совпадают
  с allowlist read-схемы; `READ_SCHEMA_VERSION` не изменился.

### S10.2 Предикаты связей в `IdentityFilter` и выдаче аренды

Зависит от `S10.1`.

- Расширить `IdentityFilter` (`api/identities.py`) и доменный `SearchQuery`
  (`domain/query.py`) предикатами связей:
  - `without_link: str | Collection[str]` — у аккаунта нет исходящей связи этой роли;
  - `not_target_of: tuple[str, str | None]` — на аккаунт никто не ссылается ролью (опц. из
    конкретного сервиса-источника);
  - `is_target_of: tuple[str, str | None]` — на аккаунт кто-то ссылается этой ролью.
- Предикаты реализуются подзапросом к связям в `PgTx` **и** `MemoryTx`, работают в
  `find`/`list`/`count` на обоих бэкендах.
- Те же условия доступны выдаче аренды: `pool.candidate_query`/`acquire` принимают их (или
  готовый `among`-`Select`), чтобы «дай любой аккаунт без privy» работало и как pull-воркер.
- Пример:
  ```python
  await space.identities.find(IdentityFilter(
      service="twitter", lifecycle="ready", not_target_of=("twitter", "privy")))
  ```
- **Руководство:** `03-accounts.md` — выборки по связям; `12-recipes.md` — рецепт кампании
  кросс-сервисной регистрации (select без privy → задача на id → `lease(among=[id])` → re-check →
  create privy + link); `api.md`, allowlist, CHANGELOG.
- `tests/test_server_admins_traceability.py` покрывает `S10.*`.
- **DoD:** `find`/`list`/`count` с `not_target_of` и `without_link` дают верный набор на обоих
  бэкендах; тот же предикат отбирает кандидатов в `pool.lease`; две параллельные кампании не
  берут один аккаунт дважды (аренда эксклюзивна); полный `bash scripts/check.sh` зелёный; все
  задачи `S10` в статусе `done` или `cancelled` с записью `D-NNN`.

---

## Фаза S11 — адрес сервиса и выбор кредов без цикла

Два отложенных запроса владельца (`D-321`): хранить URL платформы на сервисе и выбирать рабочую
креду одной строкой, без ручного перебора. Второе опирается на `last_verified_at`/
`last_rejected_at`, введённые в S9.

### S11.1 Колонка `url` у сервиса

- `Service.url: str | None = None` — адрес веб-платформы (`https://privy.io`). Необязательный и
  **не уникальный**: на одном адресе могут жить несколько сервисов, а у ssh/MTProto адреса нет.
- Нормализация в origin при объявлении сервиса (`ValueError` при старте): схема+хост+порт,
  нижний регистр, IDN→punycode, порт по умолчанию убирается, путь/query/fragment отбрасываются;
  поддержка `http://ip:port` (IPv4 без ведущих нулей, IPv6 в скобках); только `http`/`https`;
  логин/пароль в URL запрещены.
- Хранение: колонка `url text NULL` в `ih_services` (миграция после номера, занятого фазами S5–S10),
  заполняется при старте из кода, как `title`. Попадает в read-схему `hub.read.services`; админка
  показывает адрес ссылкой.
- `url` — только константа платформы; для ssh (хост на аккаунт/сервер) остаётся `None`, хост — в
  кредах/метке (`D-320`-смежное).
- **DoD:** валидный origin хранится и виден в read-схеме; `privy.io/path?x=1` нормализуется к
  `https://privy.io`; `http://1.2.3.4:8000` сохраняется как есть; невалидный URL — ошибка при
  старте; `None` допустим; оба бэкенда.

### S11.2 Доступ к кредам: `active_credential` и типизация по схеме

Зависит от S9 (поля валидности уже есть).

Две эргономические доработки доступа к кредам из адаптера/аренды.

**1. `active_credential` — первая пригодная запись без перебора.**
- Новый аксессор на `Lease` и `LoginContext`: **`active_credential(kind="default")`** → первая
  пригодная запись вида или `None`. «Пригодная» = `active`, не просрочена, **не** помечена
  `last_rejected_at`. Без приоритета «проверенной» — просто первая годная (нужна «первая
  рабочая», не «лучшая»); порядок детерминированный (по `name`).
- Пара с существующим `active_credentials()` (словарь всех активных) — та же семантика
  «активная», в единственном и множественном числе; `credential(kind, name)` остаётся точечным
  доступом.
- С `reject_credential` работает как итератор «до рабочей» без отдельного списка: отклонённая
  запись выбывает из выборки, следующий вызов отдаёт другую.
  ```python
  while (candidate := await ctx.active_credential("default")) is not None:
      if await try_sign_in(candidate.value):
          await ctx.verify_credential(candidate.kind, name=candidate.name); break
      await ctx.reject_credential(candidate.kind, name=candidate.name, code="bad")
  ```
- Сессии своего аналога не получают: у слота одна валидная сессия (`session(slot)` уже отдаёт
  только её), кандидаты сессий — это разные слоты. Зафиксировать в документации.

- **Нет пригодных или нет кредов вообще.** `active_credential` (и `credentials()`) возвращает
  `None` и когда записей нет совсем, и когда все `disabled`/`revoked`/просрочены/`reject` — это
  штатный возврат, не исключение: часть сервисов логинится без кредов (соц-логин через связь).
  Решает адаптер: нужны креды и пришёл `None` → `LoginFailure(code, retryable=False)`; вход через
  `ctx.linked(...)` → `None` игнорируется. Отличить «кредов не было» от «все сгорели» —
  `active_credentials()` (пусто) плюс `secrets.credentials()` (метаданные всех, включая `revoked`).
  Правило приёмки «не брать, когда все отклонены» (S9.2) применяется только к аккаунтам, у
  которых есть креды нужного вида и все непригодны; аккаунт с нулём кредов под него не подпадает.

**2. Типизированный доступ: generic-контекст (основной) + `schema=` (запасной).**
- Сделать `Service`, `Pool`, `Lease`, `LoginContext`, `Authenticator` **дженериками** по типам
  кредов и сессии: `Service[TCreds, TSession]`, `Pool[TCreds, TSession]`, `Lease[...]`,
  `LoginContext[...]`. Это только уровень типов; в рантайме ничего не меняется, дженерики
  стираются, схему для валидации по-прежнему держит `Service`.
- **Аренда типизируется из значения сервиса** — без аннотаций и `schema=`, потому что `Service`
  передаётся в `pool()`:
  ```python
  TELEGRAM: Service[TgCredentials, TgSession] = Service(credentials=TgCredentials, session=TgSession, ...)
  async with space.pool(TELEGRAM).lease() as lease:     # Pool[TgCredentials, TgSession]
      candidate = await lease.active_credential("default")  # → CredentialCandidate[TgCredentials] | None
      sess  = await lease.session()                     # → TgSession | None
  ```
- **Контекст входа типизируется аннотацией подписи** (один раз на функцию): внутри `login` объекта
  `Service` нет, тип приходит из сигнатуры:
  ```python
  async def tg_login(ctx: LoginContext[TgCredentials, TgSession]) -> TgSession:
      candidate = await ctx.active_credential("default")  # → CredentialCandidate[TgCredentials] | None
  ```
- **`schema=` остаётся запасным** — для именованных видов/слотов, которые один generic-параметр не
  покрывает, и для ad-hoc:
  ```python
  totp = await ctx.credential("totp", schema=TotpSecret)
  api  = await ctx.session("api", schema=ApiTokens)
  ```
  `credentials`/`credential`/`session`/`active_credential` получают `@overload` с `schema`;
  переданная схема сверяется с `Service.credential_schema(kind)`/`session_schema(slot)` (иначе
  `ValidationFailed`).
- **Совместимость:** `Service` без параметров = `Service[BaseModel, BaseModel]`; весь существующий
  код и тесты продолжают тайпчекаться и работать. Дженерики инвариантны по умолчанию — проверить
  вариантность на типах кредов/сессии и зафиксировать.

- Приёмка (`AcceptanceWorker`) и примеры адаптеров используют `active_credential`, чтобы «взять
  рабочую» было одной строкой.
- Документация: `03-accounts.md` — доступ к кредам (`credential` vs `active_credential` vs
  `active_credentials`), типизация через `schema`, оговорка про сессии; `api.md`, allowlist;
  CHANGELOG.
- `tests/test_server_admins_traceability.py` покрывает `S11.*`.
- **DoD:** `active_credential` отдаёт первую пригодную (active, не просрочена, не `reject`) и
  `None`, когда таких нет или кредов нет; доступен на `lease` и `ctx`; **generic-вывод**: в
  тесте типов `space.pool(SVC).lease().active_credential()` и `ctx.credentials()` имеют тип
  схемы сервиса без `schema=` (mypy); `schema=` типизирует именованные виды/слоты,
  несовпадение — `ValidationFailed`; код без параметров тайпчекается как прежде; адаптер берёт
  рабочую без цикла; оба бэкенда; полный `bash scripts/check.sh` зелёный; все задачи `S11` в
  статусе `done`/`cancelled` с записью `D-NNN`.

---

## Фаза S12 — интеграция с eazy_sdk

Запрос владельца (`D-322`): eazy_sdk (тулкит сборки SDK, `A:/work/stardust/lib/RespLens/eazy_sdk`)
хранит сессию через протокол `SessionStore`; хаб — надёжное шифрованное многопроцессное
хранилище. Связываем их **на стороне хаба**: eazy_sdk — общий тулкит, зависеть от
специализированного оркестратора не должен; его протокол `SessionStore` уже является швом.
Направление зависимости: хаб → eazy_sdk, опционально и лениво; eazy_sdk не меняется.

**Сверка форматов (готово).** eazy_sdk `StoredCookie.to_primitive()`:
`{name, value, domain (нижний регистр, без ведущей точки), path, host_only: bool,
expires_at: ISO|null, secure, http_only, same_site: str ("" = не задан)}`. Это **та же модель**,
что наш `Cookie` (`D-314`): одинаковые `domain` в нижнем регистре без точки и явный `host_only`;
`expires_at=None`/отсутствие = сессионная. Отличия только: срок — ISO-строка (у нас float epoch),
`same_site=""` (у нас `None`), и у eazy **нет `partition_key`** (CHIPS).

### S12.1 Формат `eazy` в конвертерах cookies и сессии

- `Cookies.dump.eazy()` / `Cookies.load.eazy(data)` — список словарей формата
  `StoredCookie.to_primitive()`. Маппинг:
  - `domain`, `path`, `host_only`, `secure`, `http_only` — один в один (конвенция совпадает);
  - срок: `dump` — float epoch → ISO в UTC, **tz-aware** (eazy требует timezone); `load` —
    `datetime.fromisoformat` → float epoch; `None`/`null` ↔ сессионная;
  - `same_site`: `dump` — `None` → `""`, enum → `"Lax"`/`"Strict"`/`"None"`; `load` — `""` → `None`,
    иначе разбор без учёта регистра;
  - `partition_key`: eazy не поддерживает — при `dump` cookie с `partition_key` даёт
    `FormatLossWarning` (значение теряется, cookie остаётся обычной), при `load` — `None`.
- `BrowserSession.dump.eazy_state()` / `load.eazy_state(CookieState)` — мост к eazy
  `CookieState` (его `to_primitive()`/`from_primitive()` — тот же список). Это `SessionBridge`
  со стороны хаба.
- **DoD:** круговой тест `Cookies.load.eazy(Cookies.dump.eazy(c)) == c` без потерь, кроме
  `partition_key` (с предупреждением); реальный `CookieState.to_primitive()` из eazy_sdk
  (golden-fixture) загружается в наш `Cookie` и обратно; tz-aware срок сохраняется; `same_site`
  пустой ↔ `None`; оба бэкенда не нужны (чистые конвертеры).

### S12.2 `lease.session_revision` и модуль `integrations.eazy`

Зависит от `S12.1`.

- **Примитив ядра `lease.session_revision`** → `int`: ревизия текущей сессии слота из
  `ih_sessions.revision`. Нужен, чтобы отдавать eazy `SessionRevision`; полезен и сам по себе.
  Добавить рядом с `lease.session()`; для пустого слота — определить (например, `0`).
- **Модуль `identity_hub.integrations.eazy`** (ленивый импорт eazy_sdk, без жёсткой
  зависимости) с `HubSessionStore`, реализующим протокол eazy `SessionStore[TSession]`:
  - конструктор принимает **уже взятую аренду** (`HubSessionStore(lease)`); аренду он **не
    создаёт и не освобождает** — её жизненный цикл у вызывающего (`async with pool.lease()`);
  - `load(key)` → `StoredSession(value, SessionRevision(lease.session_revision), cookies)`:
    `value` — модель сессии слота (`lease.session()`), `cookies` — её cookies через
    `dump.eazy_state()`; слот берётся из `SessionKey.value` (не секрет — это `(identity_id, slot)`);
  - `save(key, value, revision, cookies)` → `lease.save_session(...)`: записывает значение и
    cookies (из `CookieState` через `load.eazy_state`) в слот; ревизия сверяется с fencing;
  - `invalidate(key, expected)` → `secrets.revoke_session`/перевод слота в `invalidated`.
- Сессионная модель слота для eazy-сервиса держит и значение SDK, и cookies (например, модель с
  полями `value` и `cookies: BrowserSession`); `HubSessionStore` раскладывает `StoredSession`
  (value + cookies) по ней.
- **Граница ответственности (из обсуждения):** вход (acquire) водит хаб через `authenticator`,
  внутри которого крутится eazy_sdk; `HubSessionStore` — только хранилище под той же арендой, не
  заводит своё durable-хранилище параллельно хабу. HTTP-клиент eazy_sdk ключуется по прокси
  аренды (не на аккаунт).
- **Документация:** рецепт «eazy_sdk-SDK на хранилище хаба» в `12-recipes.md` (аренда → store →
  SDK; вход через `authenticator`, runtime-чтение через store); `api.md`, allowlist, CHANGELOG.
- `tests/test_server_admins_traceability.py` покрывает `S12.*`.
- **DoD:** `HubSessionStore(lease)` проходит контракт eazy `SessionStore` (load/save/invalidate) с
  фейковым eazy, если пакет не установлен — тест пропускается (маркер, без обязательной
  зависимости); сессия, сохранённая через store, читается хабом и наоборот; cookies переживают
  круг; `lease.session_revision` совпадает с ревизией после `save`; аренда не создаётся внутри
  store; полный `bash scripts/check.sh` зелёный; все задачи `S12` в статусе `done`/`cancelled` с
  записью `D-NNN`.

---

## Фаза S13 — единый режим хранения всех секретов

### S13.1 Распространить dual-format на все секретные хранилища

- Добавить миграцию `0037`: `payload`-колонки и CHECK ровно одного формата для прокси,
  закрытого профиля, evidence, challenge, staged-импорта, webhooks и notification channels.
- Перевести запись и чтение на общие `_seal_stored_*`/`_open_stored_*`, сохранив смешанные базы.
- Расширить `doctor.convert_secrets()` и `plaintext_secret_counts()` на все девять наборов;
  challenge context и временный answer переводить одной транзакцией.
- Downgrade разрешать только после явной конвертации всех строк обратно в encrypted.
- **DoD:** все хранилища читаются в обоих форматах, round-trip не теряет значения,
  schema/migration tests и PostgreSQL conformance зелёные.

### S13.2 Сделать plaintext режимом по умолчанию

Зависит от `S13.1`.

- Изменить дефолт `IdentityHub(secret_storage=...)` на `plaintext`; `encrypted` оставить явным
  opt-in без изменения envelope-контракта.
- В тестах, проверяющих именно шифрование, указывать `secret_storage="encrypted"` явно.
- Обновить guides, CLI и предупреждение о том, что дамп раскрывает все типы секретов.
- **DoD:** поведенческий тест подтверждает plaintext без параметра, явный encrypted и смешанная
  база продолжают работать.

---

## Фаза S17 — безопасный выбор credentials (issue 09)

### S17.1 Возвращать ключ выбранной записи вместе со значением

- Добавить публичную generic-модель `CredentialCandidate[T]` с полями `kind`, `name`, `value`
  и свойством `key -> tuple[str, str]`.
- Изменить `Lease.active_credential()` и `LoginContext.active_credential()` так, чтобы они
  возвращали `CredentialCandidate[T] | None`. Это намеренная корректировка контракта,
  выпущенного только в alpha-версии: безопасный путь должен быть путём по умолчанию.
- Сохранить правила выбора без изменений: первая по имени запись заданного вида, `active`, не
  просроченная, без `last_rejected_at` и без пометки отказа текущей попытки входа.
- Обновить overload-ы и generic-вывод в `Lease`, `LoginContext`, `_LinkedIdentity` и публичных
  протоколах. `candidate.value` должен иметь схему сервиса или явно переданный `schema=`.
- Обновить примеры адаптеров. При отказе они передают
  `credential=candidate.key` либо вызывают
  `reject_credential(candidate.kind, name=candidate.name, code=...)`.
- Добавить заметку о несовместимости в changelog и короткую миграцию с прежнего доступа
  `credentials.field` на `candidate.value.field`.
- Тесты на обоих Store: выбор второй записи после отказа первой, сохранение точного имени,
  `LoginFailure(credential=candidate.key)`, исключение записи в рамках текущей попытки,
  `None` после исчерпания, runtime-проверка `schema=` и mypy-контракт generic-типа.
- **DoD:** адаптер может получить значение и точный ключ одним вызовом; окончательный отказ
  первой записи исключает её из следующего входа; существующие правила сортировки и fencing не
  изменились.

### S17.2 Документировать временный обход и убрать его после исправления

Зависит от `S17.1`.

- До выпуска исправления поддерживается только обход с заранее известным именем через
  `ctx.credential(kind, name)` и явный `LoginFailure(credential=(kind, name))`.
- Не рекомендовать `active_credential()` без возможности сохранить ключ. Для динамически
  именованных записей честно указать, что надёжного обхода в текущем `LoginContext` нет.
- После реализации `S17.1` заменить обход в интеграционных примерах на `CredentialCandidate`.
- **DoD:** документация не предлагает повторный выбор без пометки конкретной записи; временный
  текст явно привязан к версиям до исправления.

---

## Фаза S18 — диагностическая карточка (issue 10)

### S18.1 Показывать `retryable` в `RegionInfo`

- Добавить `retryable: bool | None` в `RegionInfo` и копировать значение из `RegionState` в
  `_region_info()` для сохранённых регионов. Для синтетических колонковых регионов значение
  равно `None`.
- Проверить сериализацию карточки библиотекой и admin backend. Если схема ответа описана
  отдельно, добавить nullable-поле без переименования существующих полей.
- В интерфейсе администратора различать `retryable=True`, `False` и неизвестное `None`; не
  выводить `None` как окончательный отказ.
- Добавить conformance-тест карточки после временного и окончательного `LoginFailure`, плюс
  тест обратной совместимости сериализации.
- Обновить `docs/guide/api.md` и описание приёмки.
- **DoD:** `identities.card(id)` позволяет определить повторяемость последнего отказа `auth`, а
  карточки остальных регионов остаются корректными.

---

## Фаза S19 — точечная замена credentials (issue 11)

### S19.1 Добавить `put_credential(..., reset_auth=False)`

- Добавить keyword-only параметр `reset_auth: bool = False` в публичный API и внутренний
  `_put_credential()`.
- Вынести общий шаг сброса `auth` из `replace_credentials()` и использовать его в обеих
  командах. При `reset_auth=True` всё выполняется в одной транзакции с записью credentials и
  событием:
  - `no_session` остаётся `no_session`, но очищает `code` и `retryable`;
  - `session_invalid` переходит в `no_session` и очищает отказ;
  - другие состояния `auth` не меняются;
  - сессия не отзывается.
- `reset_auth=False` полностью сохраняет текущее поведение. Новая или заменённая запись, как и
  сейчас, очищает только собственные метки валидности.
- Проверить `expected_revision`, единый rollback и число повышений aggregate revision. Не
  добавлять миграцию схемы.
- Добавить тесты обоих Store для `no_session`, `session_invalid`, `authenticated`, значения по
  умолчанию, rollback и повторного допуска `AcceptanceWorker`/`login_many`.
- Обновить API guide, рецепт приёмки и changelog.
- **DoD:** точечная замена credentials может атомарно снять старый неповторяемый отказ, не
  отзывая сессию и не меняя поведение без явного флага.

---

## Фаза S20 — метаданные релиза и закрытие issues 09–12

### S20.1 Исправить ревизию схемы и добавить release-gate

- Исправить итоговую ревизию схемы релиза `1.0.0a3` в `CHANGELOG.md` с `0035` на `0036`.
  Упоминание миграции `0035` у конкретной функции оставить без изменений.
- Синхронизировать текст GitHub Release, потому что опубликованный wheel уже изменить нельзя;
  исправление документации не требует новой миграции.
- Расширить проверку релиза: для версии из тега секция changelog должна существовать и указывать
  тот же schema head, что `identity_hub.postgres.migrate.SCHEMA_HEAD`. Проверку запускать в
  release workflow, чтобы обычная разработка следующей миграции не делала последний релиз
  ложнокрасным.
- **DoD:** changelog и GitHub Release для `1.0.0a3` сообщают `0036`; будущая публикация падает до
  загрузки в PyPI при расхождении версии или schema head.

### S20.2 Финальная сверка и выпуск исправления

Зависит от `S17.1`, `S17.2`, `S18.1`, `S19.1`, `S20.1`.

- Обновить матрицу публичного API, changelog и traceability-тесты.
- Выполнить целевые тесты на MemoryStore/PostgreSQL, typing-тесты, admin backend/frontend и
  полный `scripts/check.sh`.
- Повторить сценарий риска: две записи, первая отклонена адаптером, следующая попытка выбирает
  вторую; после исчерпания первая больше не выбирается самопроизвольно.
- Выпустить следующую alpha/patch-версию штатным GitHub Release и проверить установку публичного
  артефакта из PyPI в чистом окружении.
- **DoD:** issue 09–12 закрыты доказательствами тестов, все задачи S17–S20 имеют статус
  `done`/`cancelled`, release workflow зелёный.

---

## 8. Матрица трассировки

| Ишью | Задачи плана |
|---|---|
| 01. Причина неудачного входа | S1.1 |
| 02. Сводка logins.failed | S0.1 |
| 03. Срок у Fault | S2.1, S2.2 |
| 04. Выход из deleted | S0.2 |
| 05. Замена credentials и auth | S3.1, S3.2 |
| 06. cool_down из quarantined | S3.1 |
| 07. Репутация при входе | S1.2 |
| 08. Публичный Observation | не выполняется |
| 09. Имя выбранной записи credentials | S17.1, S17.2 |
| 10. `retryable` в карточке | S18.1 |
| 11. `put_credential(reset_auth=...)` | S19.1 |
| 12. Ревизия схемы в changelog | S20.1 |
| Запрос владельца: временный бан | S2.2 |
| Закрытие | S4.1 |
| Запрос владельца: открытое хранение | S5.1, S5.2 |
| Запрос владельца: стандартные форматы | S6.1, S6.2, S6.3, S6.4, S6.5, S6.6 |
| Запрос владельца: приёмка импорта | S7.1, S7.2 |
| Запрос владельца: событие импорта | S8.1 |
| Запрос владельца: точечная валидность и накопление | S9.1, S9.2, S9.3, S9.4 |
| Запрос владельца: выборки по связям | S10.1, S10.2 |
| Запрос владельца: адрес сервиса и выбор кредов | S11.1, S11.2 |
| Запрос владельца: интеграция eazy_sdk | S12.1, S12.2 |
| Запрос владельца: plaintext для всех секретов | S13.1, S13.2 |
| Закрытие issues 09–12 | S20.2 |
