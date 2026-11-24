# Порядок блокировок

Требование `IMP-14`, реализация — задача `I0.2b`. Все транзакции библиотеки берут блокировки
строк PostgreSQL в одном порядке. Тогда параллельные операции ждут друг друга, а не попадают
в deadlock.

## Порядок

Перед п. 0 транзакции `Store.transaction()` берут разделяемый advisory-барьер конфигурации
прокси пространства (`identity_hub:proxy_configuration:<space_id>`). Обычные транзакции не
блокируют друг друга этим барьером. `proxies.configure_group()` берёт его эксклюзивно, только
в собственной транзакции и после drain; повышать уже взятый shared-барьер нельзя (`D-338`).
Доставка событий не держит этот барьер на время сетевого вызова. Heartbeat не меняет назначения.

0. `ih_operation_keys` — ключ идемпотентности команды (`claim_idempotency_key`). Берётся первым,
   до любых строк аккаунта: конкурент с тем же ключом ждёт здесь, ничего не удерживая.
1. `ih_identities`. Если строк несколько — по возрастанию `id` через `StoreTx.lock_identities()`.
2. Связанный аккаунт из `ih_identity_links`: зависимый раньше связанного.
3. `ih_region_states`, `ih_verifications`, `ih_credentials`, `ih_sessions`.
4. `ih_proxies` по ID, затем `ih_proxy_allocations`, `ih_bindings`.
5. `ih_leases`, `ih_lease_history`.
6. `ih_provider_operations`.
7. `ih_events`, `ih_webhook_deliveries`, `ih_notifications`.

Диспетчер и обслуживание блокируют только строки своих очередей (`SKIP LOCKED`) и никогда не
блокируют аккаунт на время доставки или сетевого вызова.

## Правила для кода

- **Несколько identity в одной транзакции** — сначала `await tx.lock_identities(ids)`: одна
  блокировка `FOR NO KEY UPDATE` по возрастанию `id`, затем работа в любом порядке. `MemoryStore`
  сериализует транзакции (`D-124`), для него метод пустой.
- **Rebind пачкой** — до первой строки блокируются все identity и весь отсортированный набор
  целевых и прежних proxy, включая временные назначения. Сортировка только ключей пачки
  не предотвращает встречный захват proxy в разных командах. Для proxy-wide отвязки набор
  identity перечитывается после proxy-lock; новая identity даёт ConcurrentModification с откатом,
  вместо захвата identity после proxy.
- **Нельзя повышать `FOR SHARE` до записи**, если параллельная транзакция может держать `FOR SHARE`
  той же строки. Две транзакции с общей разделяемой блокировкой взаимно ждут повышения до
  `UPDATE`. Так падал импорт с `on_existing="update_session"` до I0.2b; его воспроизводит
  `tests/pg/test_lock_order.py::test_reverse_order_no_deadlock`.
- **Вставки с уникальным ключом** в пачке идут в детерминированном порядке:
  `identities.create_many` сортирует по `(service, external_ref)`, см. `D-143`.
- **Сетевой I/O не выполняется под блокировкой** (правило 0.3 плана улучшений).

## Где берутся блокировки

| Операция | Что блокируется | Порядок |
|---|---|---|
| `lease.operation(kind, key=...)` | строка ключа в `ih_operation_keys`, затем окна квот | п. 0, до identity |
| `pool.lease(idempotency_key=...)` | строка ключа в `ih_operation_keys` (и на повторе) | п. 0, до выбора кандидата |
| `secrets.replace_*(idempotency_key=...)` | строка ключа, затем `expected_revision` и identity | п. 0 → п. 1 |
| Выбор кандидата аренды | строка `ih_identities`, `SKIP LOCKED` | одна строка |
| Захват аренды с proxy | identity → отсортированные proxy → allocation/binding → lease/history → outbox | reserve и lease ID связываются одной TX; legacy claim использует выбранный ID |
| Replay аренды | ключ → identity/full fence | исходные proxy/allocation читаются из lease, без выбора новой пары |
| Обычный release | identity/full fence → proxy/allocation → lease/history → outbox | исключить завершающего владельца из live-use можно только по полному fence |
| `lease.confirm_proxy` | identity/full fence → все proxy identity → allocation/binding → outbox | сначала canonical request и принятые факты; using(session) сохраняет тот же порядок |
| Итог provider-проекции | identity/full fence → все proxy команды → итоговые state/session/lifecycle → решение allocation → terminal provider claim/outbox | поздний claim откатывает проекцию и решение; конфликт поколения сохраняется как outcome |
| State/import/batch finalization | все identity пачки → весь набор proxy → итоговые факты/решение | региональные/session строки меняются только под уже захваченной своей identity; новая identity после proxy не захватывается |
| Reconcile правил привязки | все identity пачки → все proxy → ledger policy/finalizer → outbox | по одному savepoint на identity; expected generation/runtime revision, без положительного подтверждения |
| Revoke/forced yield | identity → все proxy identity → allocation → lease/history | временное место закрывается до lease-lock |
| Reclaim аренды | identity → все proxy выбранного и прежнего use → allocation → lease/history | cleanup сохраняет резерв будущего lease ID, уже вставленный reserve |
| Retention истории | отбор по сроку с исключением owner FK → удаление history | проверка owner по индексу до history-lock; allocator после history-lock не вызывается |
| Recovery ledger | bounded discovery → identity по ID `SKIP LOCKED` → proxy по ID `SKIP LOCKED` → проверка canonical use и allocation | собственная TX после окончания lease/history TX; пропущенные строки возвращаются в следующем цикле |
| Recovery счётчиков | bounded discovery proxy → proxy по ID `SKIP LOCKED` → COUNT ledger/bindings | отдельная TX до ledger recovery; identity/lease не захватываются, legacy не меняется |
| Recovery orphan history | bounded open-history discovery → identity по ID `SKIP LOCKED` → history по lease ID `SKIP LOCKED` → повторная проверка canonical ID → journal | отдельная TX; proxy/ledger/counters не меняются и не блокируются; allocator после history не вызывается |
| Lazy recovery acquire | rollback выдачи → отдельные TX сверки/ledger → повтор acquire | чужие identity никогда не захватываются после identity неудачной выдачи |
| События переходов allocator | безопасные draft в очереди TX → flush outbox перед окончанием TX/savepoint команды | очередь восстанавливается при rollback внутреннего savepoint; события не требуют ранней outbox-записи до lease/history |
| Импорт `update_session` | обновляемые identity пачки | `lock_identities`, по `id` |
| `create_many` | уникальные индексы новых строк | по `(service, external_ref)` |
| Команды `space.*` и админки с `expected_revision` | `ih_identities` (`lock_identity_revision`, `FOR UPDATE`), первой в каждой транзакции команды | п. 1, одна строка |
| Изменение тегов, меток, профиля, атрибутов, заметки (`identities.update`, `add_tags`, …) | `ih_identities`, до чтения строки | `lock_identities`, одна строка |
| `verifications.requirements.add/resolve`, шаг верификации | `ih_identities`, до чтения строки | `lock_identities`, одна строка |
| Удаление identity | identity → proxy/allocation → lease/history | п. 1 → п. 4 → п. 5 |
| Перепривязка прокси | identity, затем прокси | п. 1 → п. 4 |
| Диспетчер, вебхуки, уведомления | строки очередей, `SKIP LOCKED` | без аккаунтов |
| Истечение аренд | identity пачки (`SKIP LOCKED`) → все отсортированные proxy пачки → allocation → lease/history → outbox | только открытая история; cleanup до первой lease-lock |
