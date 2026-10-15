# Аудит issue по интеграции Identity Hub

Дата проверки: 2026-10-15. Проверенная версия: `identity_hub==1.0.0a2`, текущий рабочий код репозитория `AccountLibrarry`.

Документ предназначен для мейнтейнеров Identity Hub и команды `server-admins`. Цель проверки: отделить подтверждённые дефекты и полезные изменения API от ожидаемого поведения и уже исправленной документации.

## Решение

| № | Issue | Вердикт | Решение | Приоритет |
| --- | --- | --- | --- | --- |
| 01 | Причина неудачного входа не сохраняется | Критика обоснована | Взять в работу вместе с issue 07 | P1 |
| 02 | Сводка `logins.failed` без `code` | Дефект подтверждён, но заголовок неточен | Исправить отдельно коротким изменением | P0 |
| 03 | У `Fault` нет срока standing | Пробел API подтверждён | Взять, если временные ограничения входят в поддерживаемый сценарий | P2 |
| 04 | Наблюдение выводит аккаунт из `deleted` | Нарушен терминальный инвариант | Исправить до расширения интеграций | P0 |
| 05 | Замена credentials не сбрасывает auth-код | Проблема реальна, предложенная автоматика спорна | Взять как явную политику или документацию, не как безусловный сброс | P2 |
| 06 | `cool_down` из `quarantined` запрещён | Текущее поведение логично, но не описано | Код не менять, документировать | P3 |
| 07 | Сигналы репутации при входе теряются | Критика обоснована | Взять вместе с issue 01 | P1 |
| 08 | Пример публичного `Observation` неверен | В текущем коде уже исправлено | Закрыть как выполненное | Закрыто |

## Что стоит взять в работу сейчас

### P0: исправить измерение `logins.failed`

Issue: [02-rollups-login-code.md](02-rollups-login-code.md).

`auth.py::_login` создаёт `identity.login.failed` с `data={"error": name}`, а `rollups.py::event_contributions` читает `data["code"]`. Поэтому общий счётчик `logins.failed` создаётся, но строка rollup не содержит измерения `code`. При группировке все такие события попадают в пустую группу. Формулировка «сводка всегда пустая» слишком сильная, однако контракт аналитики действительно нарушен.

Краткосрочное исправление:

```python
code = data.get("code") or data.get("error")
```

Долгосрочно событие должен писать стабильный код из решения по issue 01. Имя Python-класса можно оставить только как обратную совместимость.

Критерии приёмки:

- `identity.login.failed` всегда даёт непустой безопасный `code`;
- `analytics.series("logins.failed", group_by="code")` возвращает фактические группы;
- отдельный тест строит событие тем же путём, что реальный `Authenticator.login`, а не вручную.

### P0: сделать `deleted` терминальным для наблюдений

Issue: [04-deleted-not-sticky.md](04-deleted-not-sticky.md).

Критика полностью подтверждается. В `domain/region_specs.py` у `Standing.DELETED` нет командных выходов, но наблюдения не проверяют таблицу командных переходов. `deleted` не входит в `sticky` или отдельное множество терминальных состояний. Поэтому наблюдение `limited`, `good` или `banned` меняет `deleted` сразу.

Это не косметика. Терминальное состояние перестаёт быть терминальным, а запоздавший webhook или результат операции может вернуть удалённый платформой аккаунт в рабочий жизненный цикл.

Рекомендуемое поведение:

- любое наблюдение с целевым состоянием, отличным от `deleted`, оставляет состояние `deleted`;
- расхождение можно фиксировать событием `identity.state.drift`;
- выход из `deleted`, если он вообще нужен, разрешается только явным административным `override` с причиной;
- добавить unit-тест машины и conformance-тест через публичный `identities.observe()`.

Не стоит добавлять `deleted` только в `confirm_leaving`: два одинаковых запоздавших наблюдения всё равно воскресили бы аккаунт.

### P1: ввести структурированную ошибку входа и обработку репутации

Issues: [01-login-failure-reason.md](01-login-failure-reason.md) и [07-login-reputation-signals.md](07-login-reputation-signals.md).

Оба issue описывают одну границу API. Сейчас `LoginChallenge` является единственным специальным исходом `Authenticator.login`. Любое другое исключение:

- переводит `auth` в `no_session`;
- записывает в событие только имя класса в поле `error`;
- записывает имя класса в `ih_provider_operations.error_code`;
- не может сообщить `standing=banned/limited`, `retryable` или машинный код отказа credentials.

Это вынуждает интеграцию создавать собственные классы исключений и отдельное состояние. Критика обоснована.

Рекомендуемое решение: один публичный структурированный тип, например `LoginFailure`, либо отдельный `login_error_classifier`. Минимальный контракт:

```python
LoginFailure(
    code="credentials_rejected",
    retryable=False,
    fault=Fault(Blame.REQUEST),
)
```

Для бана или ограничения `fault` должен позволять применить наблюдение standing тем же атомарным путём, которым завершается login operation. Произвольный `LoginContext.observe()` слабее: адаптер сможет отправить несогласованный набор наблюдений, а результат входа и изменение standing окажутся в разных транзакциях.

Требования безопасности:

- сохранять только ограниченный машинный `code`;
- не сохранять текст исключения, логин, пароль или ответ платформы без очистки;
- определить ограниченный набор исходов: credentials rejected, transient, challenge, session invalid, identity limited/banned;
- сохранить обратную совместимость для обычных исключений.

### P2: передавать срок временного standing через `Fault`

Issue: [03-fault-until.md](03-fault-until.md).

Пробел подтверждён. `Fault.retry_after` влияет только на `availability= cooling` во ветке `Blame.IDENTITY`. Ветка `Blame.IDENTITY_BANNED` создаёт standing observation без `until`. Интеграция не может одним результатом операции сообщить временное ограничение платформы.

Поле `until` у `Fault` полезно, но требует ограничения семантики:

- `until` должен передаваться в standing observation;
- дата должна содержать timezone;
- автоматическое истечение сейчас определено только для `limited -> good`;
- `banned` и `frozen` не должны выглядеть временными: их `until` библиотека сейчас не снимает;
- неверное сочетание `standing` и `until` лучше отклонять, а не молча сохранять бессмысленный срок.

Если продукту нужен именно временный бан, это отдельное изменение автомата. Простого добавления `Fault.until` недостаточно.

### P2: определить политику после замены credentials

Issue: [05-credentials-replace-auth.md](05-credentials-replace-auth.md).

Факт подтверждается: `secrets.replace_credentials()` заменяет секрет и создаёт `identity.credentials.updated`, но не меняет регион `auth`. Код предыдущего `session_invalid` остаётся до начала следующей попытки входа.

Безусловный автоматический сброс не рекомендуется. В библиотеке есть несколько credentials по `kind/name` и несколько session slot. Замена API-ключа или резервных credentials не должна автоматически менять default auth и отзывать независимую сессию.

Предпочтительные варианты:

1. Документировать, что замена credentials не меняет auth и session.
2. Добавить явный параметр или отдельную команду, например `replace_credentials(..., reset_auth=True)`.
3. Связывать credential с auth slot и сбрасывать только соответствующий slot, если такая модель будет добавлена.

Для текущего API достаточно документации, если `server-admins` запускает login сразу после замены. Отдельная команда нужна, если карточка может долго показывать устаревшую причину.

## Что не стоит менять в коде

### `quarantined -> cooling`

Issue: [06-cooldown-from-quarantined.md](06-cooldown-from-quarantined.md).

`quarantined` является более строгим состоянием доступности, чем `cooling`. Карантин снимается явным решением, а cooldown снимается таймером. Переход `quarantined -> cooling` ослабил бы карантин и вернул аккаунт в выдачу после таймера.

Текущий `TransitionNotAllowed` соответствует автомату и защищает состояние. Возврат `UNCHANGED` тоже нежелателен: вызывающий код мог бы считать, что cooldown применён.

Стоит только явно описать:

- `cool_down()` применим к `available` и `paused`, а повтор в `cooling` только продлевает срок;
- для `quarantined` команда отклоняется;
- `quarantined` снимается отдельным административным действием.

## Что уже исправлено

### Публичный `Observation`

Issue: [08-public-observation-docs.md](08-public-observation-docs.md).

В текущем `docs/guide/04-states.md` пример использует публичный контракт:

```python
await space.identities.observe(
    identity_id,
    Observation(
        region="standing",
        key="",
        state=Standing.LIMITED,
        raw_status="FLOOD_WAIT",
        code="rate_limit",
    ),
    actor="telegram-webhook-adapter",
    source=ChangeSource.WEBHOOK,
)
```

Пример соответствует `state.Observation`: `region` и `key` передаются в DTO, `actor` и `source` передаются методу, а `observed_at` можно не указывать. Дополнительная работа не нужна. Issue следует закрыть как исправленное.

## Рекомендуемый порядок реализации

1. Исправить rollup `logins.failed` и добавить интеграционный тест реального события login.
2. Закрепить терминальность `standing=deleted` тестами MemoryStore и PostgreSQL.
3. Спроектировать единый `LoginFailure` или login classifier для issues 01 и 07.
4. Добавить срок временного standing в `Fault` с валидацией поддерживаемых состояний.
5. Документировать семантику замены credentials и запрет `cool_down` из `quarantined`.
6. Добавлять явный reset auth при замене credentials только после определения связи credential и session slot.

## Проверенные источники

- `src/identity_hub/auth.py`: обработка `LoginChallenge` и общих ошибок login;
- `src/identity_hub/rollups.py`: формирование `logins.failed`;
- `src/identity_hub/domain/regions.py`: применение наблюдений, sticky и confirm-leaving;
- `src/identity_hub/domain/region_specs.py`: автоматы standing, auth и availability;
- `src/identity_hub/protocols.py`: `Fault` и `ErrorClassifier`;
- `src/identity_hub/operation.py`: применение `Fault`;
- `src/identity_hub/secrets_admin.py`: замена credentials;
- `docs/guide/04-states.md`: актуальный пример публичного `Observation`;
- `tests/unit/test_analytics.py` и `tests/unit/test_regions.py`: выполнено `141 passed`.

## Ограничения проверки

- Аудит выполнен по текущему рабочему дереву `AccountLibrarry`, а не по отдельно установленному wheel.
- Рабочее дерево содержит незакоммиченные изменения, но ключевые для issues файлы `auth.py`, `rollups.py`, `domain/regions.py`, `domain/region_specs.py`, `protocols.py` и `secrets_admin.py` в `git status` не изменены.
- Тесты `server-admins`, упомянутые авторами issue, повторно не запускались. Утверждения сверены по коду Identity Hub и его unit-тестам.

## Итог реализации

| Ишью | Задача | Реализовано | Что `server-admins` может убрать |
|---|---|---|---|
| 01 | S1.1 | `LoginFailure` сохраняет безопасный код в `auth`, событии и операции | Собственные классы исключений ради имени причины и отдельное поле последнего отказа |
| 02 | S0.1 | `identity.login.failed.code` и fallback на старое `error` в rollup | Ручную перегруппировку неудачных входов |
| 03 | S2.1, S2.2 | `Fault.until` для `limited`/`banned`, временный бан снимается таймером | Второй вызов `observe` для срока и собственный планировщик разбана |
| 04 | S0.2 | `deleted` sticky и терминален для наблюдений; выход только override | Фильтр запоздавших webhook/probe для удалённых аккаунтов |
| 05 | S3.1, S3.2 | Документирован прежний контракт и добавлен `replace_credentials(reset_auth=True)` | Отдельную команду очистки старого auth-кода после замены credentials |
| 06 | S3.1 | Запрет `quarantined → cooling` закреплён тестами и документацией | Обработку этого запрета как неизвестной ошибки; карантин снимается явным override |
| 07 | S1.2 | `LoginFailure.fault` применяет standing/auth/cooling атомарно с итогом входа | Отдельное наблюдение репутации после неудачного входа |
| 08 | — | Публичный пример `Observation` уже был корректен | Локальную поправку примера |

Новые поля и типы обратно совместимы: `identity.login.failed.error` сохранён, обычные исключения
адаптера по-прежнему дают `auth=no_session`, а сброс auth при замене credentials выключен по
умолчанию. Полный перечень контрактов и примеров — в `docs/guide/04-states.md` и
`docs/guide/05-login.md`.
