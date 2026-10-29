# identity_hub: проверка живости по внешней сессии (session-first) должна работать из коробки

**Тип:** RFC / feature request
**Компонент:** `identity_hub` (sync/probe, secrets, admissibility)
**Контекст-потребитель:** checker (`apps/checker`), сервис `armgs`
**Версия, на которой смотрели:** та, что в `.venv` на 2026-10-29

---

## TL;DR

Сценарий «у аккаунта есть сохранённая сессия (в т.ч. залитая оператором cookie), проверь живость
по ней, и если жива — считай аккаунт живым» **не выражается штатными средствами IH**. Пришлось
обходить библиотеку: проверять вход мимо IH и вручную достраивать состояние. Нужны 3 аддитивных
изменения (новый метод + параметр `slot` + послабление гейта), чтобы это стало одним честным
вызовом.

Что в IH **уже хорошо и трогать не надо:** автодетект cookie-форматов (`Cookies.load`,
`BrowserSession.load`), произвольные именованные session-слоты (`Service.session_schema(slot)` =
`session_slots.get(slot, self.session)` в `service.py:261`), аудит reveal. На этом всё и держится.

---

## Сценарий

1. Оператор заливает в аккаунт cookie-сессию в произвольный слот `cookie-N` (через
   `secrets.replace_session(..., slot="cookie-1")`). Пароля может не быть (**cookie-only**:
   `CheckerSession.value is None`, есть только `cookies`).
2. По кнопке «Проверить» нужно: **если есть валидная сессия — войти из-под неё и признать аккаунт
   живым; иначе — вход по логин:паролю**.
3. Живой аккаунт должен единообразно отображаться везде: карточка, списки, счётчики, фильтры.

## Почему сейчас «с гемором»

### Гэп 1. Probe (проверка сессии) запрещён вне `lifecycle=ready`

`domain/admissibility.py:evaluate()` безусловно, для **любой** операции, добавляет блокер при
`lifecycle != READY`:

```python
# identity_hub/domain/admissibility.py (~83)
if identity.lifecycle != Lifecycle.READY:
    blockers.append(Blocker("lifecycle", identity.lifecycle))
```

`sync.refresh(regions=("auth",))` — это операция `probe`, значит на `provisioning`/`retired`
аккаунте она падает с `OperationNotAllowed: probe ... lifecycle: provisioning`. А именно там живут
свежеимпортированные и операторские аккаунты.

При этом **вход по кредам (`auth.login`/`login_many`) на `provisioning` разрешён** (это путь
provisioning). Получается асимметрия: *креды можно проверить до ready, а сессию — нельзя*. Для
сценария, где сессия — первичное доказательство живости, это chicken-and-egg: чтобы проверить
сессию нужен `ready`, а `ready` даёт только успешный вход.

### Гэп 2. Probe смотрит только в default-слот; у `refresh` нет параметра `slot`

```python
# identity_hub/sync.py (~79) — контекст probe
async def session(self, slot: str = DEFAULT_SLOT) -> BaseModel | None: ...
# identity_hub/api/sync.py (~34)
async def refresh(self, identity_id, regions=("auth","standing")) -> RefreshResult: ...
```

Проверить конкретный именованный слот (`cookie-1`) нативно нельзя — probe всегда читает `default`.
Чтобы проверить залитую cookie, её приходится сперва копировать в `default`.

### Гэп 3. Нет способа выставить `auth=AUTHENTICATED` по уже проверенной внешней сессии

`auth` меняют только `login`/`probe`. Публичного сеттера нет:
- `secrets.replace_session(...)` пишет слот, но `auth`/`status` не трогает;
- `secrets.verify_session(slot)` / `reject_session(slot)` ставят только таймстемпы
  `last_verified_at` / `last_rejected_at` — **не двигают ни `status` слота, ни `auth` аккаунта**
  (`api/secrets.py`, ~266–298; явно сказано в докстроках).

Все списки/счётчики/фильтры «живых» у потребителя фильтруют по нативному `auth`
(`IdentityFilter(auth="authenticated")`). Если проверить сессию мимо probe, `auth` остаётся
`no_session` → **аккаунт «живой» в карточке, но отсутствует в списке живых**.

## Чем это обернулось у потребителя (текущий обход)

В checker пришлось:
1. Проверять вход **мимо IH** — прямой `ArmgsSdk` по cookie (функция `verify_armgs_session`),
   потому что нативный probe недоступен на `provisioning` и требовал armgs-токен.
2. На живой cookie — **промоутить слот в `default`**, затем `finalize→activate` (чтобы стать
   `ready`), затем `sync.refresh(auth)` **только ради побочного эффекта** выставления `auth`.
3. Это **двойная сетевая проверка** одной и той же cookie (прямая + нативный probe) и достройка
   отображения «живости» из таймстемпов слота, где `auth` не успел выставиться.

Рабочий код: `apps/checker/src/checker/adapters/identity_hub_store.py::_session_login`. Это именно
то, что хочется удалить, когда библиотека закроет гэпы.

---

## Принятое направление решения (аддитивное)

Детальный контракт, этапы и зависимости зафиксированы в плане `SF` и решении `D-334`.

### 1. `auth.verify_session(...)` — проверить конкретный session-slot

Один сетевой вызов проверяет именно указанный слот и сохраняет verdict. Успех/невалидность меняют
только metadata слота; технический сбой не помечает слот rejected. Глобальный auth и lifecycle
операция не меняет.

```python
async def verify_session(
    self,
    identity_id: UUID,
    slot: str = "default",
    *,
    actor: str,
    expected_revision: int | None = None,
) -> SessionVerificationResult: ...
```

### 2. `auth.adopt_session(...)` — принять проверенную сессию как активную

Один вызов берёт сессию выбранного слота, проверяет её provider probe и при успехе одной финальной
транзакцией делает её канонической активной сессией. Операция допустима для `provisioning` и
`ready`; `retired` требует отдельного восстановления с причиной.

```python
async def adopt_session(
    self,
    identity_id: UUID,
    slot: str = "default",
    *,
    actor: str,
    activate: bool = False,          # provisioning → ready при успехе
    expected_revision: int | None = None,
) -> SessionAdoptionResult:          # ADOPTED | INVALID | CHALLENGED | FAILED
    ...
```

Семантика: provider probe читает именно `slot`; при успехе non-default payload копируется в
канонический `default`, оба слота помечаются verified, `auth/default` становится authenticated,
а при `activate=True` выполняется `provisioning -> ready`. Это соответствует текущей модели IH,
где глобальный `identity.auth` денормализуется только из `default`. Сетевой вызов остаётся один.

Invalid non-default помечает только выбранный слот. Invalid канонического `default` переводит
глобальный auth в `session_invalid`. Timeout/transport/fencing failure не меняют auth, lifecycle
или validity metadata.

### 3. Послабление lifecycle-гейта для verify-класса операций (снимает Гэп 1 в общем виде)

Сейчас `lifecycle=READY` зашит в `evaluate()` для всех операций. Предложение — сделать допустимые
lifecycle **частью правила операции**: `OperationRule.lifecycles`; default `{READY}`, для probe
`{PROVISIONING, READY}`:

```python
# admissibility.evaluate(), вместо безусловного != READY:
allowed = service.rule(kind).lifecycles
if identity.lifecycle not in allowed:
    blockers.append(Blocker("lifecycle", identity.lifecycle))
```

Выборка кандидата и execution gate должны использовать одно и то же правило. Специальный обход
гейта внутри adoption не вводится.

### 4. (мелочь) Отразить вердикт проверки в модели слота

`verify_session`/`reject_session` сейчас только таймстемпы. Либо выставлять производное
`SessionInfo.valid: bool | None` (verified позже rejected, не revoked/expired), либо явно
задокументировать «валидность = сравнение таймстемпов», чтобы потребитель не выводил это сам
(сейчас делаем в `service.py::_session_valid`).

---

## Что НЕ нужно в библиотеке

Оркестрацию «сессия-иначе-креды-иначе-fallback» оставить потребителю. IH достаточно дать примитивы
(п.1–3); политика выбора и порядок попыток — дело checker.

## Минимально достаточно

**`auth.adopt_session`** закрывает session-first сценарий одним вызовом. **`auth.verify_session`**
закрывает ручную адресную проверку без смены активной сессии. Lifecycle rule устраняет внутреннее
противоречие между служебной выборкой probe и общим admissibility gate.

## Критерии приёмки

- [x] Для `provisioning`-аккаунта c сессией в слоте `cookie-1` один вызов проверяет вход и при
      успехе атомарно копирует её в канонический `default`, выставляет `auth=AUTHENTICATED`
      (и опц. `ready`) без второй сетевой проверки.
- [x] Cookie-only сессия (`value is None`) проверяется так же (для входа достаточно cookie).
- [x] После успеха аккаунт виден в `IdentityFilter(auth="authenticated")` (списки/счётчики).
- [x] Невалидный non-default слот: глобальный `auth` не тронут, слот помечен rejected,
      потребитель уходит на вход по кредам.
- [x] Потребитель может удалить обход `_session_login` (прямой `verify_armgs_session`, промоут в
      `default`, best-effort `refresh` ради auth) без потери поведения.

## Связанные заметки

Память потребителя: `ih-cookie-formats-and-session-slots`, `ih-provisioning-check-via-authenticator`,
`two-armgs-sdks`. Мотивацию lifecycle-гейта стоит сверить с ADR IH (docs/guide/05-login.md,
limitations.md, ADR-006/007) — возможно, verify-до-ready сознательно запрещён, тогда канонический
путь — именно `adopt_session` (п.1), который сам активирует аккаунт.

## План реализации

- Спецификация очереди: [`../session-first-plan.md`](../session-first-plan.md).
- Итоговая трассировка: [`../session-first-traceability.md`](../session-first-traceability.md).
- Реализация Identity Hub: `0dab06f`, защита stale provider call: `1dd0feb`.
- Миграция checker: точечный путь `a222b89`, batch parity `e533a5d`, dependency `4c95b68`.
- Сохраняемый checkpoint и статусы: [`../session-first-progress.md`](../session-first-progress.md).
- Архитектурное решение: `D-334` в [`../plan/DECISIONS.md`](../plan/DECISIONS.md).
